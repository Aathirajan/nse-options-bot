"""
scripts/run_backtest.py

Backtest launcher. Reads recorded/vendor ticks from the catalog, replays
them through the strategy set, and routes every emitted signal through
the Arbiter — so the exact same scoring logic that will run in PAPER/LIVE
also governs the backtest, and Net EV numbers are directly comparable.

TODO: wire this into nautilus_trader's actual BacktestEngine (venue
setup, fill simulation, latency model) once the ParquetDataCatalog write
path in catalog_wrangler.py is confirmed against your pinned
nautilus_trader version. Today, this script runs a simplified replay
loop directly against src/core/arbiter.py — good enough to validate
strategy signal quality and cost assumptions before investing in the
full Nautilus backtest wiring.

Usage:
    python scripts/run_backtest.py --instrument NIFTY-INDEX --date 2026-08-21
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.adapters.indstocks_data import RawTick
from src.core.arbiter import OpportunityArbiter, RiskLimits
from src.core.cost_engine import CostConfig, CostEngine
from src.data.catalog_wrangler import CatalogWrangler
from src.strategies.obi_momentum import OBIMomentumStrategy
from src.strategies.vol_breakout import VolBreakoutStrategy
from src.strategies.vwap_reversion import VWAPReversionStrategy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_settings(path: str = "config/settings.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_strategies(instrument_id: str, settings: dict) -> list:
    """
    Instantiate the strategy set for a single instrument. Extend this to
    loop over settings["instruments"]["watchlist"] for a multi-instrument
    backtest. ORPC is session-open-only and needs CE/PE tokens, so it's
    wired up separately in run_live.py rather than here.
    """
    return [
        OBIMomentumStrategy(instrument_token=instrument_id, quantity=1),
        VWAPReversionStrategy(instrument_token=instrument_id, quantity=1),
        VolBreakoutStrategy(instrument_token=instrument_id, quantity=1),
    ]


def run_backtest(instrument_id: str, partition_date: str, settings: dict) -> None:
    wrangler = CatalogWrangler(settings["catalog"]["path"])
    df = wrangler.read_ticks(instrument_id, partition_date)
    logger.info("Loaded %d ticks for %s on %s", len(df), instrument_id, partition_date)

    cost_engine = CostEngine(CostConfig(**settings["costs"]))
    risk_limits = RiskLimits(
        net_ev_threshold_bps=settings["risk"]["net_ev_threshold_bps"],
        max_concurrent_positions=settings["risk"]["max_concurrent_positions"],
        max_daily_loss_inr=settings["risk"]["max_daily_loss_inr"],
        max_position_size_units=settings["risk"]["max_position_size_units"],
    )
    arbiter = OpportunityArbiter(cost_engine, risk_limits)

    strategies = build_strategies(instrument_id, settings)
    for s in strategies:
        s.on_session_start()

    signals_emitted = 0
    signals_approved = 0

    for row in df.itertuples():
        tick = RawTick(
            ws_token=instrument_id,
            ltp=row.price,
            timestamp_ns=row.timestamp_ns,
            bid=getattr(row, "bid", None),
            ask=getattr(row, "ask", None),
            bid_qty=getattr(row, "bid_size", None),
            ask_qty=getattr(row, "ask_size", None),
            volume=getattr(row, "volume", None),
        )

        for strategy in strategies:
            signal = strategy.on_tick(tick)
            if signal is None:
                continue

            signals_emitted += 1
            spread_pct = 0.5
            if tick.bid and tick.ask and tick.bid > 0:
                spread_pct = ((tick.ask - tick.bid) / tick.bid) * 100

            decision = arbiter.evaluate_single(
                signal,
                notional_inr=tick.ltp * signal.quantity,
                bid_ask_spread_pct=spread_pct,
                expected_slippage_pct=0.1,
                is_option="CE" in instrument_id or "PE" in instrument_id,
            )
            status = "APPROVED" if decision.approved else "rejected"
            logger.info(
                "[%s] %s %s qty=%d net_ev=%.1fbps cost=%.1fbps (%s)",
                status,
                signal.strategy_id,
                signal.direction.value,
                signal.quantity,
                decision.net_ev_bps,
                decision.estimated_cost_bps,
                decision.reason,
            )
            if decision.approved:
                signals_approved += 1

    logger.info(
        "Backtest complete: %d signals emitted, %d approved (%.1f%%)",
        signals_emitted,
        signals_approved,
        (signals_approved / signals_emitted * 100) if signals_emitted else 0.0,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", required=True, help="Nautilus instrument id, e.g. NIFTY-INDEX")
    parser.add_argument("--date", required=True, help="Partition date, e.g. 2026-08-21")
    parser.add_argument("--config", default="config/settings.yaml")
    args = parser.parse_args()

    settings = load_settings(args.config)
    run_backtest(args.instrument, args.date, settings)


if __name__ == "__main__":
    main()
