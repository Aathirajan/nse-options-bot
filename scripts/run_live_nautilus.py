"""
scripts/run_live_nautilus.py

Real nautilus_trader TradingNode launcher — assembles the actual live
engine (data engine, exec engine, risk engine, portfolio, reconciliation)
with IndstocksLiveMarketDataClient / IndstocksLiveExecutionClient
registered via their factories, and runs one of our strategies inside
it through the OpportunityEngineStrategy bridge.

This graduates run_live.py's standalone asyncio loop (which works, and
is simpler to debug) to the full Nautilus live stack, which additionally
gives you: automatic position/account reconciliation on reconnect,
proper order state machine enforcement, and the same
generate_order_report()-style tooling used in backtests.

Config construction confirmed against nautilus_trader==1.231.0
(TradingNodeConfig's real struct fields, inspected directly) — not
guessed.

Usage:
    export INDSTOCKS_ACCESS_TOKEN=...
    python scripts/run_live_nautilus.py --mode PAPER --instrument NIFTY-INDEX
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from nautilus_trader.common.config import Environment
from nautilus_trader.config import (
    CacheConfig,
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    LoggingConfig,
)
from nautilus_trader.live.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId

from src.adapters.depth_poller import DepthPoller
from src.adapters.mapper import InstrumentMapper
from src.adapters.indstocks_exec import IndstocksExecutionClient
from src.adapters.indstocks_market_data import IndstocksMarketDataClient
from src.adapters.nautilus_factories import (
    IndstocksDataClientConfig,
    IndstocksExecClientConfig,
    IndstocksLiveDataClientFactory,
    IndstocksLiveExecClientFactory,
)
from src.core.arbiter import OpportunityArbiter, PositionState, RiskLimits
from src.core.cost_engine import CostConfig, CostEngine
from src.core.signals import Direction
from src.strategies.base import BaseStrategy
from src.strategies.nautilus_bridge import (
    OpportunityEngineStrategy,
    OpportunityEngineStrategyConfig,
)
from src.strategies.obi_momentum import OBIMomentumStrategy
from src.strategies.opening_dislocation import OpeningRangePremiumConvergence
from src.strategies.vol_breakout import VolBreakoutStrategy
from src.strategies.vwap_reversion import VWAPReversionStrategy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CLIENT_NAME = "INDSTOCKS"

HALT_MARKER_FILE = Path("HALTED")


def load_settings(path: str = "config/settings.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_watchlist_ws_tokens(settings: dict, mapper: InstrumentMapper) -> list[str]:
    """Resolve every configured watchlist index, refusing partial startup."""
    tokens = []
    for symbol in settings["instruments"]["watchlist"]:
        instrument_key = f"{symbol.replace(' ', '').upper()}-INDEX"
        try:
            tokens.append(mapper.to_ws_token(instrument_key))
        except (KeyError, ValueError) as exc:
            raise RuntimeError(
                f"Watchlist instrument '{symbol}' has no valid WS token. "
                "Refusing partial live startup; confirm ws_token_override first."
            ) from exc
    return tokens


def build_node_config(settings: dict, dry_run: bool) -> TradingNodeConfig:
    mapper = InstrumentMapper(settings["instruments"]["instrument_master_path"])
    watchlist_tokens = build_watchlist_ws_tokens(settings, mapper)
    data_client_config = IndstocksDataClientConfig(
        access_token=os.environ["INDSTOCKS_ACCESS_TOKEN"],
        instrument_master_path=settings["instruments"]["instrument_master_path"],
        ws_url=settings["indstocks"]["ws_prices_url"],
        initial_ws_tokens=tuple(watchlist_tokens),
    )
    exec_client_config = IndstocksExecClientConfig(
        access_token=os.environ["INDSTOCKS_ACCESS_TOKEN"],
        instrument_master_path=settings["instruments"]["instrument_master_path"],
        rest_base_url=settings["indstocks"]["rest_base_url"],
        ws_orders_url=settings["indstocks"]["ws_orders_url"],
        dry_run=dry_run,
    )

    return TradingNodeConfig(
        environment=Environment.LIVE,
        trader_id=TraderId("OPPORTUNITY-ENGINE-001"),
        logging=LoggingConfig(log_level="INFO"),
        cache=CacheConfig(),
        data_engine=LiveDataEngineConfig(),
        risk_engine=LiveRiskEngineConfig(),
        exec_engine=LiveExecEngineConfig(reconciliation=True),
        data_clients={CLIENT_NAME: data_client_config},
        exec_clients={CLIENT_NAME: exec_client_config},
    )


async def resolve_orpc_bridges(
    settings: dict, mapper: InstrumentMapper, arbiter: OpportunityArbiter,
) -> list[OpportunityEngineStrategy]:
    """Resolve and construct one ORPC bridge per enabled watchlist symbol."""
    config = settings.get("strategies", {}).get("orpc", {})
    if not config.get("enabled", False):
        return []

    market_data = IndstocksMarketDataClient(
        access_token=os.environ["INDSTOCKS_ACCESS_TOKEN"],
        rest_base_url=settings["indstocks"]["rest_base_url"],
    )
    bridges = []
    try:
        for symbol in settings["instruments"]["watchlist"]:
            underlying = symbol.replace(" ", "").upper()
            underlying_record = mapper.get_record(f"{underlying}-INDEX")
            expiries = await market_data.list_expiries(underlying=underlying)
            if not expiries:
                raise RuntimeError(f"No upcoming expiries returned for {underlying}")
            chain = await market_data.option_chain(
                exchange=underlying_record.exchange,
                segment="INDEX",
                underlying_scrip=underlying_record.security_id,
                expiry=expiries[0],
                strike_count=1,
            )
            ce_leg, pe_leg = chain.strikes[chain.atm_strike()]
            if ce_leg is None or pe_leg is None:
                logger.warning("ORPC skipped for %s: ATM pair incomplete", underlying)
                continue
            prefix = "NFO" if underlying_record.exchange == "NSE" else "BFO"
            if not ce_leg.security_id or not pe_leg.security_id:
                logger.warning(
                    "ORPC skipped for %s: option-chain returned blank contract IDs",
                    underlying,
                )
                continue
            ce_record = mapper.from_ws_token_string(f"{prefix}:{ce_leg.security_id}")
            pe_record = mapper.from_ws_token_string(f"{prefix}:{pe_leg.security_id}")
            if ce_record is None or pe_record is None:
                logger.warning(
                    "ORPC skipped for %s: refresh missing ATM contracts %s/%s",
                    underlying, ce_leg.security_id, pe_leg.security_id,
                )
                continue
            from src.adapters.instrument_builder import build_instrument
            ce_id = str(build_instrument(ce_record).id)
            pe_id = str(build_instrument(pe_record).id)
            bridges.append(build_orpc_strategy(
                arbiter, ce_id, pe_id, config.get("quantity", 1),
            ))
            logger.info("ORPC startup ATM: %s expiry=%s strike=%s", underlying, expiries[0], chain.atm_strike())
    finally:
        await market_data.close()
    return bridges


def build_arbiter(settings: dict) -> OpportunityArbiter:
    cost_engine = CostEngine(CostConfig(**settings["costs"]))
    risk_limits = RiskLimits(
        net_ev_threshold_bps=settings["risk"]["net_ev_threshold_bps"],
        max_concurrent_positions=settings["risk"]["max_concurrent_positions"],
        max_daily_loss_inr=settings["risk"]["max_daily_loss_inr"],
        max_position_size_units=settings["risk"]["max_position_size_units"],
    )
    return OpportunityArbiter(cost_engine, risk_limits)


def _bridge(
    inner: BaseStrategy,
    instrument_id: str,
    arbiter: OpportunityArbiter,
    on_halt=None,
) -> OpportunityEngineStrategy:
    return OpportunityEngineStrategy(
        config=OpportunityEngineStrategyConfig(instrument_id=instrument_id),
        inner_strategy=inner,
        arbiter=arbiter,
        on_halt=on_halt,
    )


def build_strategies(
    settings: dict,
    arbiter: OpportunityArbiter,
    orpc: OpportunityEngineStrategy | None = None,
    on_halt=None,
) -> list[OpportunityEngineStrategy]:
    """Instantiate all enabled strategy/instrument combinations."""
    mapper = InstrumentMapper(settings["instruments"]["instrument_master_path"])
    from src.adapters.instrument_builder import build_instrument
    enabled = settings.get("strategies", {})
    strategy_types = {
        "obi_momentum": OBIMomentumStrategy,
        "vwap_reversion": VWAPReversionStrategy,
        "vol_breakout": VolBreakoutStrategy,
    }
    strategies = []
    for symbol in settings["instruments"]["watchlist"]:
        instrument_key = f"{symbol.replace(' ', '').upper()}-INDEX"
        record = mapper.get_record(instrument_key)
        instrument_id = str(build_instrument(record).id)
        for name, strategy_type in strategy_types.items():
            config = enabled.get(name, {})
            if config.get("enabled", False):
                strategies.append(_bridge(
                    strategy_type(instrument_id, config.get("quantity", 1)),
                    instrument_id,
                    arbiter,
                    on_halt=on_halt,
                ))
    if orpc is not None:
        strategies.append(orpc)
    return strategies


def build_orpc_strategy(
    arbiter: OpportunityArbiter,
    ce_instrument_id: str,
    pe_instrument_id: str,
    quantity: int = 1,
    on_halt=None,
) -> OpportunityEngineStrategy:
    """Build ORPC on both resolved option legs using the shared Arbiter."""
    inner = OpeningRangePremiumConvergence(
        atm_ce_token=ce_instrument_id,
        atm_pe_token=pe_instrument_id,
        quantity=quantity,
    )
    return OpportunityEngineStrategy(
        config=OpportunityEngineStrategyConfig(instrument_id=ce_instrument_id),
        inner_strategy=inner,
        arbiter=arbiter,
        is_option=True,
        additional_instrument_ids=(pe_instrument_id,),
        on_halt=on_halt,
    )


def build_strategy(settings: dict, instrument_key: str) -> OpportunityEngineStrategy:
    """Backward-compatible single OBI strategy builder for existing callers."""
    mapper = InstrumentMapper(settings["instruments"]["instrument_master_path"])
    record = mapper.get_record(instrument_key)
    from src.adapters.instrument_builder import build_instrument
    instrument_id = str(build_instrument(record).id)
    return _bridge(
        OBIMomentumStrategy(instrument_id, 1), instrument_id, build_arbiter(settings)
    )


async def _create_flatten_order_nautilus(
    exec_client: IndstocksExecutionClient,
    mapper: InstrumentMapper,
    position: PositionState,
    is_option: bool,
    dry_run: bool,
) -> None:
    """Create and submit an order to flatten a position."""
    record = mapper.from_ws_token_string(position.instrument_id)
    if record is None:
        logger.error("No instrument record for '%s' — cannot create flatten order", position.instrument_id)
        return
    flatten_side = "SELL" if position.direction == Direction.BUY else "BUY"
    from src.adapters.indstocks_exec import OrderRequest
    order_req = OrderRequest(
        security_id=record.security_id,
        exchange=record.exchange,
        segment="DERIVATIVE" if is_option else "EQUITY",
        side=flatten_side,
        quantity=position.quantity,
        order_type="MARKET",
        remarks=f"FLATTEN/{position.instrument_id}"[:100],
    )
    result = await exec_client.place_order(order_req, dry_run=dry_run)
    logger.critical(
        "[%s] Flatten order submitted for %s: side=%s qty=%s order_id=%s",
        "DRY RUN" if dry_run else "LIVE",
        position.instrument_id,
        order_req.side,
        order_req.quantity,
        result.order_id,
    )


async def flatten_all_and_halt_nautilus(
    arbiter: OpportunityArbiter,
    exec_client: IndstocksExecutionClient,
    mapper: InstrumentMapper,
    dry_run: bool,
    reason: str,
) -> None:
    """
    Cancel all open orders and flatten all open positions using the Nautilus execution client.
    This is called when daily loss limit is breached or on graceful shutdown.
    """
    logger.critical("TRADING HALT INITIATED: %s", reason)
    arbiter.halt(reason)

    # Write halt marker file for persistence across restarts
    try:
        timestamp = datetime.now(timezone.utc).astimezone().isoformat()
        HALT_MARKER_FILE.write_text(f"Halted at {timestamp} — {reason}\n")
        logger.critical("Halt marker file written: %s", HALT_MARKER_FILE)
    except OSError as exc:
        logger.critical("Failed to write halt marker file: %s", exc)

    # Fetch fresh open orders from broker
    open_order_ids = []
    try:
        order_book = await exec_client.get_order_book()
        for order in order_book:
            status = str(order.get("status", "")).upper()
            if status in {"QUEUED", "O-PENDING", "SL-PENDING", "PROCESSING", "INITIATED", "PENDING", "PARTIALLY FILLED"}:
                order_id = order.get("id") or order.get("order_id")
                if order_id:
                    open_order_ids.append(str(order_id))
    except Exception as exc:  # noqa: BLE001
        logger.critical("Failed to fetch order book for cancellation: %s", exc)

    # Cancel all open orders
    cancelled_count = 0
    for order_id in open_order_ids:
        try:
            for segment in ("DERIVATIVE", "EQUITY"):
                try:
                    await exec_client.cancel_order(order_id, segment, dry_run=dry_run)
                    logger.critical("[%s] Cancelled order %s", "DRY RUN" if dry_run else "LIVE", order_id)
                    cancelled_count += 1
                    break
                except Exception:
                    continue
            else:
                logger.critical("Could not determine segment for order %s, cancel skipped", order_id)
        except Exception as exc:  # noqa: BLE001
            logger.critical("Failed to cancel order %s: %s", order_id, exc)

    # Fetch fresh positions from broker and flatten
    flattened_count = 0
    try:
        for segment, product in (("derivative", "margin"), ("equity", "intraday")):
            positions = await exec_client.get_positions(segment, product)
            for position in positions:
                net_qty = int(position.get("net_qty") or 0)
                if net_qty != 0:
                    security_id = position.get("security_id")
                    exchange = position.get("exchange", "NSE")
                    record = None
                    for rec in mapper._by_security_id.values():
                        if rec.security_id == security_id and rec.exchange == exchange:
                            record = rec
                            break
                    if record:
                        pos_state = PositionState(
                            instrument_id=record.nautilus_id,
                            quantity=abs(net_qty),
                            direction=Direction.BUY if net_qty > 0 else Direction.SELL,
                            average_entry_price=float(position.get("avg_price") or 0),
                        )
                        await _create_flatten_order_nautilus(exec_client, mapper, pos_state, record.instrument_type == "OPTION", dry_run)
                        flattened_count += 1
    except Exception as exc:  # noqa: BLE001
        logger.critical("Failed to fetch/flatten positions: %s", exc)

    logger.critical(
        "Halt complete: cancelled %d orders, flattened %d positions (dry_run=%s)",
        cancelled_count, flattened_count, dry_run
    )


async def reconcile_with_broker_nautilus(
    arbiter: OpportunityArbiter,
    exec_client: IndstocksExecutionClient,
    mapper: InstrumentMapper,
    *,
    halt_on_mismatch: bool = True,
    initialize: bool = False,
) -> tuple[int, float]:
    """
    Reconcile arbiter state with broker.
    If initialize=True, initialize arbiter state from broker (used at startup).
    Returns (actual_open_positions, actual_realized_pnl).
    """
    positions = []
    for segment, product in (("derivative", "margin"), ("equity", "intraday")):
        positions.extend(await exec_client.get_positions(segment, product))
    actual_count = sum(1 for position in positions if int(position.get("net_qty") or 0) != 0)
    actual_pnl = sum(float(position.get("realized_profit") or 0) for position in positions)

    if initialize:
        arbiter_positions = []
        for position in positions:
            net_qty = int(position.get("net_qty") or 0)
            if net_qty != 0:
                security_id = position.get("security_id")
                exchange = position.get("exchange", "NSE")
                record = None
                for rec in mapper._by_security_id.values():
                    if rec.security_id == security_id and rec.exchange == exchange:
                        record = rec
                        break
                if record:
                    pos_state = PositionState(
                        instrument_id=record.nautilus_id,
                        quantity=abs(net_qty),
                        direction=Direction.BUY if net_qty > 0 else Direction.SELL,
                        average_entry_price=float(position.get("avg_price") or 0),
                    )
                    arbiter_positions.append(pos_state)
        arbiter.initialize_from_broker(actual_count, actual_pnl, arbiter_positions)
        logger.info(
            "Arbiter initialized from broker: open_positions=%d, realized_pnl=%.2f",
            actual_count, actual_pnl
        )
        return actual_count, actual_pnl

    if arbiter.reconcile_open_positions(actual_count, actual_pnl):
        return actual_count, actual_pnl

    message = (
        f"Arbiter/broker mismatch: local open_positions={arbiter.state.open_positions}, "
        f"broker={actual_count}, local realized_pnl={arbiter.state.realized_pnl:.2f}, "
        f"broker={actual_pnl:.2f}"
    )
    logger.critical(message)
    if halt_on_mismatch:
        raise RuntimeError(message)
    return actual_count, actual_pnl


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", type=str.upper, choices=["PAPER", "LIVE"], default="PAPER"
    )
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument(
        "--no-flatten-on-exit",
        action="store_true",
        help="Do not flatten positions/cancel orders on graceful shutdown (Ctrl+C). Default is to flatten.",
    )
    args = parser.parse_args()

    if "INDSTOCKS_ACCESS_TOKEN" not in os.environ:
        raise SystemExit("Set INDSTOCKS_ACCESS_TOKEN before running")

    dry_run = args.mode != "LIVE"
    if not dry_run:
        logger.warning(
            "Running in LIVE mode — real orders will be submitted through "
            "the Nautilus execution client. Confirm PAPER mode first."
        )

    settings = load_settings(args.config)
    mapper = InstrumentMapper(settings["instruments"]["instrument_master_path"])
    arbiter = build_arbiter(settings)

    # Check for halt marker file from previous session
    if HALT_MARKER_FILE.exists():
        halt_reason = HALT_MARKER_FILE.read_text().strip()
        logger.critical(
            "HALT MARKER FILE FOUND: Previous session was halted (%s). "
            "Manual intervention required — delete %s to resume trading.",
            halt_reason, HALT_MARKER_FILE
        )
        if not dry_run:
            raise SystemExit("Refusing to start in LIVE mode with halt marker present. Delete HALTED file to proceed.")
        else:
            # PAPER mode continues: no real orders are placed, so continuing is safe
            # for debugging/validation. User must still manually delete HALTED file
            # to confirm they've investigated the prior halt.
            logger.warning("PAPER mode: continuing despite halt marker (delete %s to clear)", HALT_MARKER_FILE)

    # Create execution client for reconciliation (will be reused by Nautilus factory)
    exec_client = IndstocksExecutionClient(
        access_token=os.environ["INDSTOCKS_ACCESS_TOKEN"],
        rest_base_url=settings["indstocks"]["rest_base_url"],
        ws_orders_url=settings["indstocks"]["ws_orders_url"],
    )

    # MANDATORY: Reconcile with broker BEFORE building/starting the node
    logger.info("Performing mandatory startup reconciliation with broker...")
    try:
        asyncio.run(reconcile_with_broker_nautilus(arbiter, exec_client, mapper, initialize=True, halt_on_mismatch=True))
        logger.info("Startup reconciliation successful — building TradingNode")
    except Exception as exc:  # noqa: BLE001
        logger.critical("Startup reconciliation failed: %s", exc)
        raise SystemExit("Refusing to start: mandatory broker reconciliation failed") from exc

    orpc_bridges = asyncio.run(resolve_orpc_bridges(settings, mapper, arbiter))
    node_config = build_node_config(settings, dry_run=dry_run)

    node = TradingNode(config=node_config)
    node.add_data_client_factory(CLIENT_NAME, IndstocksLiveDataClientFactory)
    node.add_exec_client_factory(CLIENT_NAME, IndstocksLiveExecClientFactory)
    node.build()

    # Get the execution client from the node for shutdown handling
    nautilus_exec_client = node.exec_clients[CLIENT_NAME]._client

    # Callback for when arbiter halts during tick processing
    async def on_halt_callback(arb: OpportunityArbiter, reason: str) -> None:
        await flatten_all_and_halt_nautilus(arb, nautilus_exec_client, mapper, dry_run, reason)

    for strategy in build_strategies(settings, arbiter, orpc=orpc_bridges[0] if orpc_bridges else None, on_halt=on_halt_callback):
        node.trader.add_strategy(strategy)
    for strategy in orpc_bridges[1:]:
        node.trader.add_strategy(strategy)

    shutdown_in_progress = False

    async def graceful_shutdown(sig_name: str) -> None:
        nonlocal shutdown_in_progress
        if shutdown_in_progress:
            logger.warning("Shutdown already in progress, ignoring %s", sig_name)
            return
        shutdown_in_progress = True
        logger.critical("Graceful shutdown initiated by %s", sig_name)
        if not args.no_flatten_on_exit:
            await flatten_all_and_halt_nautilus(arbiter, nautilus_exec_client, mapper, dry_run, f"graceful shutdown ({sig_name})")
        else:
            logger.warning("--no-flatten-on-exit specified: skipping position flattening on shutdown")
            arbiter.halt(f"graceful shutdown ({sig_name}) — positions NOT flattened per flag")
        # Stop the node
        node.stop()
        logger.critical("Graceful shutdown complete")

    def signal_handler(sig_name: str) -> None:
        asyncio.create_task(graceful_shutdown(sig_name))

    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: signal_handler(s.name))
            except NotImplementedError:
                pass
    except RuntimeError:
        # No event loop running yet (e.g., Windows)
        pass

    try:
        node.run()
    finally:
        if not shutdown_in_progress and not arbiter.is_halted():
            if not args.no_flatten_on_exit:
                asyncio.run(flatten_all_and_halt_nautilus(arbiter, nautilus_exec_client, mapper, dry_run, "normal exit"))
            else:
                logger.warning("--no-flatten-on-exit specified: skipping position flattening on normal exit")
        node.dispose()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received during startup/shutdown")
