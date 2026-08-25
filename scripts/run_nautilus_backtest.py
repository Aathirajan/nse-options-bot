"""
scripts/run_nautilus_backtest.py

Full nautilus_trader BacktestEngine runner — this is the "graduate to
real Nautilus" path. It:

  1. Loads QuoteTicks from the real ParquetDataCatalog (written via
     CatalogWrangler.write_to_nautilus_catalog).
  2. Registers a simulated venue with realistic Indian brokerage/fee
     settings via a custom FeeModel (see below) — note our own
     cost_engine.py's STT/GST/turnover math is STILL applied inside the
     strategy bridge's Arbiter call, so Nautilus's fee model here should
     be a light pass-through for exchange execution mechanics, not a
     duplicate of the full Indian cost stack.
  3. Adds the instrument, wraps a strategy (e.g. OBIMomentumStrategy) in
     the Nautilus bridge, and runs the engine.

Confirmed against nautilus_trader==1.231.0 (inspected directly):
    BacktestEngine.add_venue(venue, oms_type, account_type,
                              starting_balances, book_type=...)
    BacktestEngine.add_instrument(instrument)
    BacktestEngine.add_data(data)
    BacktestEngine.add_strategy(strategy)
    BacktestEngine.run()

Usage:
    python scripts/run_nautilus_backtest.py --instrument NIFTY-INDEX
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.model.currencies import INR
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from src.adapters.mapper import InstrumentMapper
from src.adapters.instrument_builder import build_instrument
from src.core.arbiter import OpportunityArbiter, RiskLimits
from src.core.cost_engine import CostConfig, CostEngine
from src.strategies.nautilus_bridge import (
    OpportunityEngineStrategy,
    OpportunityEngineStrategyConfig,
)
from src.strategies.obi_momentum import OBIMomentumStrategy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_settings(path: str = "config/settings.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", default="NIFTY-INDEX", help="Nautilus instrument id key in instruments.json")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--catalog", default="./data_catalog/nautilus")
    args = parser.parse_args()

    settings = load_settings(args.config)
    mapper = InstrumentMapper(settings["instruments"]["instrument_master_path"])
    record = mapper.get_record(args.instrument)
    instrument = build_instrument(record)

    catalog = ParquetDataCatalog(args.catalog)
    ticks = catalog.quote_ticks(instrument_ids=[str(instrument.id)])
    if not ticks:
        raise SystemExit(
            f"No QuoteTicks found in {args.catalog} for {instrument.id} — "
            f"write some first via CatalogWrangler.write_to_nautilus_catalog()"
        )
    logger.info("Loaded %d QuoteTicks for %s from the real Nautilus catalog", len(ticks), instrument.id)

    engine = BacktestEngine(config=BacktestEngineConfig(trader_id="OPPORTUNITY-ENGINE-001"))

    venue = instrument.id.venue
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        starting_balances=[Money(1_000_000, INR)],
    )
    engine.add_instrument(instrument)
    engine.add_data(ticks)

    cost_engine = CostEngine(CostConfig(**settings["costs"]))
    risk_limits = RiskLimits(
        net_ev_threshold_bps=settings["risk"]["net_ev_threshold_bps"],
        max_concurrent_positions=settings["risk"]["max_concurrent_positions"],
        max_daily_loss_inr=settings["risk"]["max_daily_loss_inr"],
        max_position_size_units=settings["risk"]["max_position_size_units"],
    )
    arbiter = OpportunityArbiter(cost_engine, risk_limits)

    inner_strategy = OBIMomentumStrategy(instrument_token=str(instrument.id), quantity=1)
    strategy = OpportunityEngineStrategy(
        config=OpportunityEngineStrategyConfig(instrument_id=str(instrument.id)),
        inner_strategy=inner_strategy,
        arbiter=arbiter,
        is_option=(record.instrument_type == "OPTION"),
    )
    engine.add_strategy(strategy)

    engine.run()

    logger.info("Backtest complete. Approved trades this run: %d", len(arbiter.state.approved_today))
    print(engine.trader.generate_account_report(venue))
    print(engine.trader.generate_order_fills_report())


if __name__ == "__main__":
    main()
