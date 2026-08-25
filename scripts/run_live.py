"""
scripts/run_live.py

LIVE/PAPER launcher: connects the INDstocks price feed, runs every
incoming tick through the strategy set, routes signals through the
Arbiter, and — only for approved signals — submits orders via the
execution client. dry_run defaults to True everywhere: flip it only
after you've watched a full session of dry-run decisions and are
confident in the signal quality and cost assumptions.

Usage:
    export INDSTOCKS_ACCESS_TOKEN=...
    python scripts/run_live.py --mode PAPER
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

from src.adapters.depth_poller import DepthPoller
from src.adapters.indstocks_data import IndstocksPriceFeed, RawTick
from src.adapters.indstocks_exec import IndstocksExecutionClient, OrderRequest
from src.adapters.indstocks_market_data import IndstocksMarketDataClient
from src.adapters.mapper import InstrumentMapper
from src.core.arbiter import OpportunityArbiter, RiskLimits
from src.core.cost_engine import CostConfig, CostEngine
from src.core.signals import Direction
from src.strategies.base import BaseStrategy
from src.strategies.obi_momentum import OBIMomentumStrategy
from src.strategies.opening_dislocation import OpeningRangePremiumConvergence
from src.strategies.vol_breakout import VolBreakoutStrategy
from src.strategies.vwap_reversion import VWAPReversionStrategy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HALT_MARKER_FILE = Path("HALTED")


def load_settings(path: str = "config/settings.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def resolve_orpc_strategy(
    settings: dict,
    mapper: InstrumentMapper,
    market_data: IndstocksMarketDataClient,
    quantity: int,
    underlying_symbol: str = "NIFTY",
) -> OpeningRangePremiumConvergence:
    """
    Resolves today's ATM CE/PE contracts for `underlying_symbol` and
    builds an OpeningRangePremiumConvergence strategy wired to them.

    Uses two confirmed-real endpoints:
      - GET /market/instruments/expiries -> nearest expiry (data[0],
        ascending order per docs)
      - GET /market/option-chain -> the strike ladder, from which we
        take the strike closest to underlying_ltp as "ATM" (a simple
        and reasonable proxy — the option chain doesn't label a strike
        as ATM explicitly, so this is our own choice, not something
        the API asserts)

    WS tokens for the resolved CE/PE contracts are built as
    "NFO:{security_id}" (NSE F&O) — confirmed safe because, unlike
    indices, F&O contracts' WS token IS derivable from their REST
    security_id (see mapper.py's module docstring for the index-only
    exception).
    """
    record = mapper.get_record(f"{underlying_symbol}-INDEX")

    expiries = await market_data.list_expiries(underlying=underlying_symbol)
    if not expiries:
        raise RuntimeError(f"No upcoming expiries returned for {underlying_symbol}")
    nearest_expiry = expiries[0]

    chain = await market_data.option_chain(
        exchange=record.exchange,
        segment="INDEX",
        underlying_scrip=record.security_id,
        expiry=nearest_expiry,
        strike_count=1,  # only need the ATM strike and its immediate neighbors
    )
    atm_strike = chain.atm_strike()
    ce_leg, pe_leg = chain.strikes[atm_strike]
    if ce_leg is None or pe_leg is None:
        raise RuntimeError(f"ATM strike {atm_strike} missing a CE or PE leg in the chain response")

    ws_prefix = "NFO" if record.exchange == "NSE" else "BFO"
    atm_ce_token = f"{ws_prefix}:{ce_leg.security_id}"
    atm_pe_token = f"{ws_prefix}:{pe_leg.security_id}"

    logger.info(
        "ORPC resolved: expiry=%s atm_strike=%s ce=%s(%s) pe=%s(%s)",
        nearest_expiry, atm_strike, ce_leg.trading_symbol, atm_ce_token,
        pe_leg.trading_symbol, atm_pe_token,
    )

    return OpeningRangePremiumConvergence(
        atm_ce_token=atm_ce_token, atm_pe_token=atm_pe_token, quantity=quantity,
    )


def build_engine(settings: dict, mapper: InstrumentMapper) -> tuple[
    OpportunityArbiter, IndstocksExecutionClient, list[BaseStrategy]
]:
    cost_engine = CostEngine(CostConfig(**settings["costs"]))
    risk_limits = RiskLimits(
        net_ev_threshold_bps=settings["risk"]["net_ev_threshold_bps"],
        max_concurrent_positions=settings["risk"]["max_concurrent_positions"],
        max_daily_loss_inr=settings["risk"]["max_daily_loss_inr"],
        max_position_size_units=settings["risk"]["max_position_size_units"],
    )
    arbiter = OpportunityArbiter(cost_engine, risk_limits)

    exec_client = IndstocksExecutionClient(
        access_token=os.environ["INDSTOCKS_ACCESS_TOKEN"],
        rest_base_url=settings["indstocks"]["rest_base_url"],
        ws_orders_url=settings["indstocks"]["ws_orders_url"],
    )

    watchlist = settings["instruments"]["watchlist"]
    watchlist_tokens: dict[str, str] = {}
    for symbol in watchlist:
        instrument_key = f"{symbol.replace(' ', '').upper()}-INDEX"
        try:
            watchlist_tokens[instrument_key] = mapper.to_ws_token(instrument_key)
        except (KeyError, ValueError) as exc:
            raise RuntimeError(
                f"Watchlist instrument '{symbol}' has no valid WS token. "
                "Refresh instruments.json and confirm the index token before starting."
            ) from exc

    nifty_ws_token = watchlist_tokens.get("NIFTY-INDEX")
    if nifty_ws_token is None:
        raise RuntimeError("The configured watchlist must include NIFTY")
    strategies: list[BaseStrategy] = [
        OBIMomentumStrategy(instrument_token=nifty_ws_token, quantity=1),
        VWAPReversionStrategy(instrument_token=nifty_ws_token, quantity=1),
        VolBreakoutStrategy(instrument_token=nifty_ws_token, quantity=1),
    # OpeningRangePremiumConvergence needs live ATM CE/PE tokens for
    # the day, resolved at session start from the option chain (spot
    # price -> nearest strike). See resolve_orpc_strategy() below --
    # called separately in main() since it needs an async HTTP call
    # before this synchronous strategy list can be finalized.
    ]

    return arbiter, exec_client, strategies


def build_watchlist_ws_tokens(settings: dict, mapper: InstrumentMapper) -> list[str]:
    """Resolve every configured index to a confirmed WS token."""
    tokens = []
    for symbol in settings["instruments"]["watchlist"]:
        instrument_key = f"{symbol.replace(' ', '').upper()}-INDEX"
        try:
            tokens.append(mapper.to_ws_token(instrument_key))
        except (KeyError, ValueError) as exc:
            raise RuntimeError(
                f"Watchlist instrument '{symbol}' has no valid WS token. "
                "Refusing partial live startup."
            ) from exc
    return tokens


def subscribe_orpc_tokens(feed: IndstocksPriceFeed, strategy: OpeningRangePremiumConvergence) -> list[str]:
    """Add the resolver's current CE/PE contracts to the live feed."""
    tokens = [strategy._atm_ce_token, strategy._atm_pe_token]
    feed.subscribe(tokens)
    return tokens


def _create_flatten_order(
    mapper: InstrumentMapper,
    position: "PositionState",
    is_option: bool,
) -> OrderRequest | None:
    """Create an order request to flatten a position (opposite side, same quantity)."""
    record = mapper.from_ws_token_string(position.instrument_id)
    if record is None:
        logger.error("No instrument record for '%s' — cannot create flatten order", position.instrument_id)
        return None
    flatten_side = "SELL" if position.direction == Direction.BUY else "BUY"
    return OrderRequest(
        security_id=record.security_id,
        exchange=record.exchange,
        segment="DERIVATIVE" if is_option else "EQUITY",
        side=flatten_side,
        quantity=position.quantity,
        order_type="MARKET",
        remarks=f"FLATTEN/{position.instrument_id}"[:100],
    )


async def flatten_all_and_halt(
    arbiter: OpportunityArbiter,
    exec_client: IndstocksExecutionClient,
    mapper: InstrumentMapper,
    dry_run: bool,
    reason: str,
) -> None:
    """
    Cancel all open orders and flatten all open positions.
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
            # We need to determine segment for each order - try both
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
    positions_to_flatten = []
    try:
        for segment, product in (("derivative", "margin"), ("equity", "intraday")):
            positions = await exec_client.get_positions(segment, product)
            for position in positions:
                net_qty = int(position.get("net_qty") or 0)
                if net_qty != 0:
                    # Get instrument details from position
                    security_id = position.get("security_id")
                    exchange = position.get("exchange", "NSE")
                    # We need to find the instrument_id from our mapper
                    # The position has security_id, we can reverse-lookup
                    record = None
                    for rec in mapper._by_security_id.values():
                        if rec.security_id == security_id and rec.exchange == exchange:
                            record = rec
                            break
                    if record:
                        from src.core.arbiter import PositionState
                        pos_state = PositionState(
                            instrument_id=record.nautilus_id,
                            quantity=abs(net_qty),
                            direction=Direction.BUY if net_qty > 0 else Direction.SELL,
                            average_entry_price=float(position.get("avg_price") or 0),
                        )
                        positions_to_flatten.append((pos_state, record.instrument_type == "OPTION"))
    except Exception as exc:  # noqa: BLE001
        logger.critical("Failed to fetch positions for flattening: %s", exc)

    # Submit flatten orders
    flattened_count = 0
    for pos_state, is_option in positions_to_flatten:
        try:
            order_req = _create_flatten_order(mapper, pos_state, is_option)
            if order_req:
                result = await exec_client.place_order(order_req, dry_run=dry_run)
                logger.critical(
                    "[%s] Flatten order submitted for %s: side=%s qty=%s order_id=%s",
                    "DRY RUN" if dry_run else "LIVE",
                    pos_state.instrument_id,
                    order_req.side,
                    order_req.quantity,
                    result.order_id,
                )
                flattened_count += 1
        except Exception as exc:  # noqa: BLE001
            logger.critical("Failed to flatten position %s: %s", pos_state.instrument_id, exc)

    logger.critical(
        "Halt complete: cancelled %d orders, flattened %d positions (dry_run=%s)",
        cancelled_count, flattened_count, dry_run
    )


async def reconcile_with_broker(
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
        # Build PositionState objects from broker positions
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
                    from src.core.arbiter import PositionState
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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["PAPER", "LIVE"], default="PAPER")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument(
        "--no-flatten-on-exit",
        action="store_true",
        help="Do not flatten positions/cancel orders on graceful shutdown (Ctrl+C). Default is to flatten.",
    )
    args = parser.parse_args()

    settings = load_settings(args.config)
    dry_run = args.mode != "LIVE"
    if not dry_run:
        logger.warning(
            "Running in LIVE mode — real orders will be submitted. "
            "Confirm you've validated PAPER mode first."
        )

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

    mapper = InstrumentMapper(settings["instruments"]["instrument_master_path"])
    arbiter, exec_client, strategies = build_engine(settings, mapper)

    # Create and start DepthPoller for real spread/depth data
    market_data = IndstocksMarketDataClient(
        access_token=os.environ["INDSTOCKS_ACCESS_TOKEN"],
        rest_base_url=settings["indstocks"]["rest_base_url"],
    )
    depth_poller = DepthPoller(market_data, settings, mapper)
    
    # Add watchlist instruments to depth poller
    for symbol in settings["instruments"]["watchlist"]:
        instrument_key = f"{symbol.replace(' ', '').upper()}-INDEX"
        try:
            ws_token = mapper.to_ws_token(instrument_key)
            depth_poller.add_instrument(ws_token)
        except (KeyError, ValueError) as exc:
            logger.error("Failed to add %s to depth poller: %s", symbol, exc)

    orpc = None
    try:
        orpc = await resolve_orpc_strategy(settings, mapper, market_data, quantity=1)
        strategies.append(orpc)
        # Add ORPC option legs to depth poller
        if orpc:
            depth_poller.add_instrument(orpc._atm_ce_token)
            depth_poller.add_instrument(orpc._atm_pe_token)
    except Exception:  # noqa: BLE001 — ORPC is optional; don't block the other strategies
        logger.exception("Failed to resolve ORPC's ATM CE/PE for today — running without it")
    finally:
        await market_data.close()

    for s in strategies:
        s.on_session_start()

    async def on_order_update(update) -> None:
        arbiter.handle_fill(update)
        logger.info("Order update: %s", update)

    exec_client.add_order_update_handler(on_order_update)

    # Track shutdown state to prevent new signal processing during shutdown
    shutdown_in_progress = False

    async def on_tick(tick: RawTick) -> None:
        nonlocal shutdown_in_progress
        if shutdown_in_progress or arbiter.is_halted():
            return
        
        # Enrich tick with real depth data for OBI momentum
        depth = depth_poller.get_latest_depth(tick.ws_token)
        if depth:
            tick.bid = depth.bid_price
            tick.ask = depth.ask_price
            tick.bid_qty = depth.bid_qty
            tick.ask_qty = depth.ask_qty
        
        for strategy in strategies:
            signal = strategy.on_tick(tick)
            if signal is None:
                continue

            # Use real polled spread if available, else fallback to tick spread, else default
            spread_pct = 0.5
            signal_depth = depth_poller.get_latest_depth(signal.instrument_id)
            if signal_depth:
                spread_pct = signal_depth.spread_pct
            elif tick.bid and tick.ask and tick.bid > 0:
                spread_pct = ((tick.ask - tick.bid) / tick.bid) * 100

            record = mapper.from_ws_token_string(signal.instrument_id)
            is_option = record is not None and record.instrument_type == "OPTION"

            decision = arbiter.evaluate_single(
                signal,
                notional_inr=tick.ltp * signal.quantity,
                bid_ask_spread_pct=spread_pct,
                expected_slippage_pct=0.1,
                is_option=is_option,
            )

            if not decision.approved:
                logger.info(
                    "Rejected %s %s: %s (net_ev=%.1fbps)",
                    signal.strategy_id,
                    signal.direction.value,
                    decision.reason,
                    decision.net_ev_bps,
                )
                # Check if arbiter was halted due to daily loss breach
                if arbiter.is_halted():
                    logger.critical("Arbiter halted: %s", arbiter.get_halt_reason())
                    await flatten_all_and_halt(
                        arbiter, exec_client, mapper, dry_run, arbiter.get_halt_reason() or "daily loss limit breached"
                    )
                    shutdown_in_progress = True
                continue

            logger.info(
                "APPROVED %s %s qty=%d net_ev=%.1fbps -> submitting order (dry_run=%s)",
                signal.strategy_id,
                signal.direction.value,
                signal.quantity,
                decision.net_ev_bps,
                dry_run,
            )

            if record is None:
                logger.error(
                    "No instrument record for '%s' — cannot place order "
                    "(mapper is stale or signal used a raw token not in "
                    "instruments.json), skipping.", signal.instrument_id,
                )
                continue

            order_request = OrderRequest(
                security_id=record.security_id,
                exchange=record.exchange,
                segment="DERIVATIVE" if is_option else "EQUITY",
                side=signal.direction.value,
                quantity=signal.quantity,
                order_type="LIMIT" if signal.limit_price else "MARKET",
                limit_price=signal.limit_price,
                remarks=f"{signal.strategy_id}/{signal.timestamp_ns}"[:100],
            )
            result = await exec_client.place_order(order_request, dry_run=dry_run)
            arbiter.register_order(result.order_id, decision, tick.ltp)

    feed = IndstocksPriceFeed(
        access_token=os.environ["INDSTOCKS_ACCESS_TOKEN"],
        ws_url=settings["indstocks"]["ws_prices_url"],
        mode="quote",
        reconnect_backoff=settings["indstocks"]["reconnect_backoff_seconds"],
    )
    feed.add_handler(on_tick)

    # MANDATORY: Reconcile with broker BEFORE subscribing to feed / starting signal processing
    logger.info("Performing mandatory startup reconciliation with broker...")
    try:
        await reconcile_with_broker(arbiter, exec_client, mapper, initialize=True, halt_on_mismatch=True)
        logger.info("Startup reconciliation successful — starting price feed")
    except Exception as exc:  # noqa: BLE001
        logger.critical("Startup reconciliation failed: %s", exc)
        raise SystemExit("Refusing to start: mandatory broker reconciliation failed") from exc

    feed.subscribe(build_watchlist_ws_tokens(settings, mapper))
    if orpc is not None:
        subscribe_orpc_tokens(feed, orpc)

    # Start depth poller
    await depth_poller.start()

    # Graceful shutdown handler
    async def graceful_shutdown(sig_name: str) -> None:
        nonlocal shutdown_in_progress
        if shutdown_in_progress:
            logger.warning("Shutdown already in progress, ignoring %s", sig_name)
            return
        shutdown_in_progress = True
        logger.critical("Graceful shutdown initiated by %s", sig_name)
        if not args.no_flatten_on_exit:
            await flatten_all_and_halt(
                arbiter, exec_client, mapper, dry_run, f"graceful shutdown ({sig_name})"
            )
        else:
            logger.warning("--no-flatten-on-exit specified: skipping position flattening on shutdown")
            arbiter.halt(f"graceful shutdown ({sig_name}) — positions NOT flattened per flag")
        # Stop feed, exec client, and depth poller
        feed.stop()
        exec_client.stop()
        await depth_poller.stop()
        # Wait a bit for in-flight operations to complete
        await asyncio.sleep(1)
        logger.critical("Graceful shutdown complete")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(graceful_shutdown(s.name)))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler for SIGTERM
            pass

    async def reconciliation_loop() -> None:
        interval = settings.get("risk", {}).get("reconciliation_interval_seconds", 300)
        while not shutdown_in_progress:
            await asyncio.sleep(interval)
            if shutdown_in_progress:
                break
            try:
                await reconcile_with_broker(arbiter, exec_client, mapper, halt_on_mismatch=True)
            except Exception as exc:  # noqa: BLE001
                logger.critical("Periodic reconciliation failed: %s", exc)
                if not shutdown_in_progress:
                    await flatten_all_and_halt(arbiter, exec_client, mapper, dry_run, f"reconciliation failure: {exc}")
                    shutdown_in_progress = True

    try:
        await asyncio.gather(
            feed.run_forever(),
            exec_client.run_order_updates_forever(),
            reconciliation_loop(),
        )
    finally:
        if not shutdown_in_progress and not arbiter.is_halted():
            # Normal exit without explicit shutdown signal — still flatten unless flagged
            if not args.no_flatten_on_exit:
                await flatten_all_and_halt(arbiter, exec_client, mapper, dry_run, "normal exit")
            else:
                logger.warning("--no-flatten-on-exit specified: skipping position flattening on normal exit")
        await depth_poller.stop()
        await exec_client.close()


if __name__ == "__main__":
    if "INDSTOCKS_ACCESS_TOKEN" not in os.environ:
        raise SystemExit("Set INDSTOCKS_ACCESS_TOKEN before running")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received during startup/shutdown")
