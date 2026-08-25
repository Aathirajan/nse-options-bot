"""
tests/test_nautilus_integration.py

Proves the full real-Nautilus pipeline works end-to-end: instrument
building, writing to a real ParquetDataCatalog, running an actual
BacktestEngine, and submitting real orders through the strategy bridge
when the arbiter approves a signal.

Requires nautilus_trader to be installed (pip install nautilus_trader).
Run with:
    python tests/test_nautilus_integration.py
"""

from __future__ import annotations

import random
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters.instrument_builder import build_instrument
from src.adapters.mapper import InstrumentMapper
from src.core.arbiter import OpportunityArbiter, RiskLimits
from src.core.cost_engine import CostConfig, CostEngine
from src.data.catalog_wrangler import CatalogWrangler, NormalizedTick
from src.strategies.nautilus_bridge import OpportunityEngineStrategy, OpportunityEngineStrategyConfig
from src.strategies.obi_momentum import OBIMomentumStrategy


def _make_synthetic_ticks(n: int = 200, seed: int = 7) -> list[NormalizedTick]:
    random.seed(seed)
    ticks = []
    price = 24500.0
    for i in range(n):
        price += random.uniform(-2, 2)
        ticks.append(
            NormalizedTick(
                nautilus_instrument_id="NIFTY-INDEX",
                timestamp_ns=1_755_000_000_000_000_000 + i * 1_000_000_000,
                price=round(price, 2),
                volume=random.randint(1, 50),
                bid=round(price - 0.5, 2),
                ask=round(price + 0.5, 2),
                bid_size=random.randint(50, 500),
                ask_size=random.randint(50, 500),
                source="synthetic_test",
            )
        )
    return ticks


def test_instrument_builder_index():
    mapper = InstrumentMapper("config/instruments.json")
    record = mapper.get_record("NIFTY-INDEX")
    instrument = build_instrument(record)
    assert instrument.id.symbol.value == "NIFTY"
    assert str(instrument.id.venue) == "NSE"


def test_instrument_builder_option():
    mapper = InstrumentMapper("config/instruments.json")
    record = mapper.get_record("NIFTY26AUG24500CE-NFO")
    instrument = build_instrument(record)
    assert float(instrument.strike_price) == 24500.0


def test_catalog_write_and_read_back():
    from nautilus_trader.persistence.catalog import ParquetDataCatalog

    tmp_dir = tempfile.mkdtemp()
    try:
        mapper = InstrumentMapper("config/instruments.json")
        record = mapper.get_record("NIFTY-INDEX")
        instrument = build_instrument(record)

        wrangler = CatalogWrangler(tmp_dir)
        ticks = _make_synthetic_ticks(50)
        wrangler.write_to_nautilus_catalog(ticks, instrument)

        catalog = ParquetDataCatalog(str(Path(tmp_dir) / "nautilus"))
        read_back = catalog.quote_ticks(instrument_ids=[str(instrument.id)])
        assert len(read_back) == 50
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_full_backtest_engine_runs_and_can_submit_orders():
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.model.currencies import INR
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.objects import Money

    tmp_dir = tempfile.mkdtemp()
    try:
        mapper = InstrumentMapper("config/instruments.json")
        record = mapper.get_record("NIFTY-INDEX")
        instrument = build_instrument(record)

        wrangler = CatalogWrangler(tmp_dir)
        ticks_raw = _make_synthetic_ticks(200)
        wrangler.write_to_nautilus_catalog(ticks_raw, instrument)

        from nautilus_trader.persistence.catalog import ParquetDataCatalog
        catalog = ParquetDataCatalog(str(Path(tmp_dir) / "nautilus"))
        quote_ticks = catalog.quote_ticks(instrument_ids=[str(instrument.id)])
        assert len(quote_ticks) == 200

        engine = BacktestEngine(config=BacktestEngineConfig(trader_id="TEST-001"))
        venue = instrument.id.venue
        engine.add_venue(
            venue=venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            starting_balances=[Money(1_000_000, INR)],
        )
        engine.add_instrument(instrument)
        engine.add_data(quote_ticks)

        cost_engine = CostEngine(CostConfig())
        # Force approval to prove the order path itself works, independent
        # of whether synthetic random-walk data happens to clear a
        # realistic EV threshold (it usually won't — see run_backtest.py).
        risk_limits = RiskLimits(net_ev_threshold_bps=-1000.0)
        arbiter = OpportunityArbiter(cost_engine, risk_limits)

        inner = OBIMomentumStrategy(instrument_token=str(instrument.id), quantity=1, cooldown_ticks=5)
        strategy = OpportunityEngineStrategy(
            config=OpportunityEngineStrategyConfig(instrument_id=str(instrument.id)),
            inner_strategy=inner,
            arbiter=arbiter,
            is_option=False,
        )
        engine.add_strategy(strategy)
        engine.run()

        assert len(arbiter.state.approved_today) > 0, "expected at least one approved+submitted order"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    passed, failed = 0, 0
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL: {test.__name__} -> {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
