"""
tests/test_core_logic.py

Covers everything that doesn't need live INDstocks/TrueData credentials:
cost engine, arbiter, OBI, VWAP, mapper, and each strategy's signal logic
against synthetic ticks. Run with:

    python -m pytest tests/ -v

or standalone:

    python tests/test_core_logic.py
"""

from __future__ import annotations

import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters.indstocks_data import RawTick
from src.adapters.mapper import InstrumentMapper
from src.core.arbiter import OpportunityArbiter, RiskLimits
from src.core.cost_engine import CostConfig, CostEngine
from src.core.signals import Direction, OpportunitySignal
from src.features.obi_calculator import OBIStream, top_of_book_obi
from src.features.vwap_engine import AnchoredVWAP
from src.strategies.obi_momentum import OBIMomentumStrategy
from src.strategies.opening_dislocation import DynamicOpeningRangePremiumConvergence, IST
from src.strategies.vol_breakout import VolBreakoutStrategy
from src.strategies.vwap_reversion import VWAPReversionStrategy


def test_cost_engine_basic():
    engine = CostEngine(CostConfig())
    breakdown = engine.estimate_cost_bps(
        direction=Direction.SELL,
        notional_inr=9000,
        bid_ask_spread_pct=0.5,
        expected_slippage_pct=0.1,
        is_option=True,
    )
    assert breakdown.stt_bps > 0  # SELL + option -> STT applies
    assert breakdown.total_bps > 0


def test_cost_engine_buy_no_stt():
    engine = CostEngine(CostConfig())
    breakdown = engine.estimate_cost_bps(
        direction=Direction.BUY,
        notional_inr=9000,
        bid_ask_spread_pct=0.5,
        expected_slippage_pct=0.1,
        is_option=True,
    )
    assert breakdown.stt_bps == 0  # BUY -> no options STT
    assert breakdown.stamp_duty_bps > 0  # BUY -> stamp duty applies


def test_arbiter_rejects_low_ev():
    engine = CostEngine(CostConfig())
    arbiter = OpportunityArbiter(engine, RiskLimits(net_ev_threshold_bps=15.0))
    sig = OpportunitySignal(
        strategy_id="test",
        instrument_id="X",
        direction=Direction.BUY,
        raw_alpha_bps=5.0,  # tiny alpha
        confidence=0.5,
        estimated_duration_sec=10,
        quantity=1,
    )
    decision = arbiter.score(
        sig, notional_inr=1000, bid_ask_spread_pct=1.0,
        expected_slippage_pct=0.5, is_option=False,
    )
    assert decision.approved is False


def test_arbiter_approves_high_ev():
    engine = CostEngine(CostConfig())
    arbiter = OpportunityArbiter(engine, RiskLimits(net_ev_threshold_bps=15.0))
    sig = OpportunitySignal(
        strategy_id="test",
        instrument_id="X",
        direction=Direction.BUY,
        raw_alpha_bps=500.0,  # large alpha, non-option so costs stay small
        confidence=0.9,
        estimated_duration_sec=10,
        quantity=1,
    )
    decision = arbiter.score(
        sig, notional_inr=100_000, bid_ask_spread_pct=0.05,
        expected_slippage_pct=0.01, is_option=False,
    )
    assert decision.approved is True


def test_arbiter_batch_ranking_and_position_limit():
    engine = CostEngine(CostConfig())
    arbiter = OpportunityArbiter(
        engine, RiskLimits(net_ev_threshold_bps=1.0, max_concurrent_positions=1)
    )
    high = arbiter.score(
        OpportunitySignal("a", "X", Direction.BUY, 500.0, 0.9, 10, 1),
        notional_inr=100_000, bid_ask_spread_pct=0.05,
        expected_slippage_pct=0.01, is_option=False,
    )
    low = arbiter.score(
        OpportunitySignal("b", "Y", Direction.BUY, 400.0, 0.9, 10, 1),
        notional_inr=100_000, bid_ask_spread_pct=0.05,
        expected_slippage_pct=0.01, is_option=False,
    )
    final = arbiter.evaluate_batch([low, high])  # deliberately out of order
    # higher-EV signal should be ranked first and take the one slot
    assert final[0].signal.strategy_id == "a"
    assert final[0].approved is True
    assert final[1].approved is False  # slot already taken


def test_obi_basic():
    assert top_of_book_obi(100, 100) == 0.0
    assert top_of_book_obi(150, 50) == 0.5
    assert top_of_book_obi(0, 0) == 0.0

    stream = OBIStream(window=3)
    stream.update(100, 0)
    stream.update(100, 0)
    stream.update(0, 100)
    assert abs(stream.smoothed_obi - (1 / 3)) < 1e-9


def test_vwap_basic():
    vwap = AnchoredVWAP()
    vwap.update(100, 10)
    vwap.update(102, 10)
    assert vwap.vwap == 101.0
    band = vwap.band(1.0)
    assert band is not None
    assert band[0] < vwap.vwap < band[1]


def test_obi_momentum_strategy_emits_on_strong_imbalance():
    strat = OBIMomentumStrategy(
        instrument_token="X", quantity=1, obi_entry_threshold=0.3,
        obi_window=3, cooldown_ticks=2,
    )
    strat.on_session_start()
    signal = None
    for i in range(5):
        signal = strat.on_tick(
            RawTick("X", ltp=100, timestamp_ns=i, bid_qty=100, ask_qty=10)
        )
    assert signal is not None
    assert signal.direction == Direction.BUY


def test_vwap_reversion_strategy_emits_on_stretch():
    strat = VWAPReversionStrategy(
        instrument_token="X", quantity=1, entry_std_dev=1.0, min_ticks_before_entry=5,
    )
    strat.on_session_start()
    signal = None
    for i in range(5):
        signal = strat.on_tick(RawTick("X", ltp=100, timestamp_ns=i, volume=10))
    assert signal is None  # flat prices, no deviation yet
    signal = strat.on_tick(RawTick("X", ltp=150, timestamp_ns=99, volume=10))
    assert signal is not None
    assert signal.direction == Direction.SELL  # price above VWAP -> fade down


def test_vol_breakout_strategy_emits_on_squeeze_break():
    strat = VolBreakoutStrategy(
        instrument_token="X", quantity=1, lookback_ticks=5,
        squeeze_range_bps_max=100.0, breakout_buffer_bps=1.0,
    )
    strat.on_session_start()
    signal = None
    for i, price in enumerate([100.0, 100.1, 99.9, 100.0, 100.1]):
        signal = strat.on_tick(RawTick("X", ltp=price, timestamp_ns=i))
    assert signal is None  # still filling lookback window
    signal = strat.on_tick(RawTick("X", ltp=110.0, timestamp_ns=99))
    assert signal is not None
    assert signal.direction == Direction.BUY


def test_orpc_resolves_at_start_and_on_date_change():
    resolved_dates = []

    def resolver(session_date):
        resolved_dates.append(session_date)
        suffix = session_date.strftime("%d")
        return f"CE-{suffix}", f"PE-{suffix}"

    strategy = DynamicOpeningRangePremiumConvergence(resolver, quantity=1)
    strategy.on_session_start()
    startup_date = resolved_dates[0]
    next_date = startup_date + dt.timedelta(days=1)
    next_day_ns = int(
        dt.datetime.combine(next_date, dt.time(9, 16), tzinfo=IST).timestamp() * 1e9
    )

    strategy.on_tick(RawTick(f"CE-{next_date:%d}", 100, next_day_ns))
    assert resolved_dates == [startup_date, next_date]
    assert strategy._atm_ce_token == f"CE-{next_date:%d}"


def test_mapper_roundtrip():
    mapper = InstrumentMapper("config/instruments.json")
    ws_token = mapper.to_ws_token("NIFTY-INDEX")
    # NIFTY-INDEX uses ws_token_override (NIDX: prefix, different number
    # than security_id) -- confirmed necessary per INDstocks' own docs
    # warning that index WS/REST tokens are NOT interchangeable.
    assert ws_token == "NIDX:26000"
    assert mapper.to_nautilus_id(ws_token) == "NIFTY-INDEX"
    assert mapper.to_security_id("NIFTY-INDEX") == "40000001"
    assert mapper.to_rest_scrip_code("NIFTY-INDEX") == "NSE_40000001"


def test_refresh_matches_real_index_names():
    from scripts.refresh_instruments import build_index_records

    rows = [
        {"exch": "NSE", "index_name": "NIFTY 50", "security_id": "40000001"},
        {"exch": "NSE", "index_name": "BANK NIFTY", "security_id": "40000003"},
        {"exch": "BSE", "index_name": "SENSEX", "security_id": "40000006"},
        {"exch": "NSE", "index_name": "NIFTY 100", "security_id": "40000002"},
    ]

    records = build_index_records(
        rows,
        ["NIFTY", "SENSEX"],
        {"NIFTY50": "NIDX:26000"},
    )

    assert [record["nautilus_id"] for record in records] == [
        "NIFTY-INDEX",
        "SENSEX-INDEX",
    ]


def test_refresh_matches_fno_rows_without_symbol_name():
    from scripts.refresh_instruments import build_fno_records

    rows = [
        {
            "EXCH": "NSE",
            "SECURITY_ID": "93785",
            "INSTRUMENT_NAME": "OPTSTK",
            "TRADING_SYMBOL": "NIFTY-Sep2026-25000-CE",
            "SYMBOL_NAME": "",
            "OPTION_TYPE": "CE",
            "STRIKE_PRICE": "25000.00000",
            "EXPIRY_DATE": "09/29/2026 14:00",
            "TICK_SIZE": "5",
            "LOT_UNITS": "75",
        }
    ]

    records = build_fno_records(rows, ["NIFTY"])

    assert len(records) == 1
    assert records[0]["symbol"] == "NIFTY"
    assert records[0]["security_id"] == "93785"
    assert records[0]["instrument_type"] == "OPTION"
    assert records[0]["expiry"] == "2026-09-29"
    assert records[0]["nautilus_id"] == "NIFTY26SEP25000CE-NFO"


def test_mapper_index_without_ws_override_raises():
    mapper = InstrumentMapper("config/instruments.json")
    # SENSEX-INDEX has no ws_token_override in the fixture -- ws_token
    # must raise rather than silently derive a likely-wrong token.
    record = mapper.get_record("SENSEX-INDEX")
    try:
        _ = record.ws_token
        raise AssertionError("expected ValueError for missing ws_token_override")
    except ValueError:
        pass


def test_option_chain_parsing_against_real_docs_example():
    from src.adapters.indstocks_market_data import IndstocksMarketDataClient, OptionChainResult

    # This is the EXACT worked example from INDstocks' own API docs
    # (Options Toolkit / Option Chain), used to verify parsing logic
    # without needing a live connection.
    body = {
        "underlying_ltp": 24471.7,
        "expiry": "2026-08-18",
        "strikes": {
            "24450": {
                "ce": {
                    "security_id": "45108", "trading_symbol": "NIFTY-Aug2026-24450-CE",
                    "last_price": 167.9, "previous_close_price": 274.95, "oi": 1608490,
                    "previous_oi": 1606988, "volume": 9079330, "top_bid_price": 166.05,
                    "top_bid_quantity": 195, "top_ask_price": 167.5, "top_ask_quantity": 130,
                    "iv": 10.5, "greeks": {"delta": 0.56, "gamma": 0.0011, "theta": -10.04, "vega": 13.39},
                },
                "pe": {
                    "security_id": "45109", "trading_symbol": "NIFTY-Aug2026-24450-PE",
                    "last_price": 117.65, "previous_close_price": 88.9, "oi": 1579370,
                    "previous_oi": 1579129, "volume": 11974495, "top_bid_price": 117.65,
                    "top_bid_quantity": 195, "top_ask_price": 119.4, "top_ask_quantity": 195,
                    "iv": 10.4, "greeks": {"delta": -0.44, "gamma": 0.0011, "theta": -9.95, "vega": 13.39},
                },
            }
        },
    }
    strikes = {
        float(strike_str): (
            IndstocksMarketDataClient._parse_leg(legs.get("ce")),
            IndstocksMarketDataClient._parse_leg(legs.get("pe")),
        )
        for strike_str, legs in body["strikes"].items()
    }
    result = OptionChainResult(underlying_ltp=body["underlying_ltp"], expiry=body["expiry"], strikes=strikes)

    assert result.atm_strike() == 24450.0
    side, leg = result.cheaper_leg_at(24450.0)
    assert side == "PE"  # 117.65 < 167.9
    assert leg.security_id == "45109"


def test_resolve_orpc_strategy_wires_atm_tokens_correctly():
    """
    Proves scripts/run_live.py's resolve_orpc_strategy() correctly turns
    a mocked option-chain response into WS tokens for the ORPC strategy
    -- exercising the full expiry-lookup -> option-chain -> ATM-strike ->
    ws-token pipeline without needing live credentials.
    """
    import asyncio
    from unittest.mock import AsyncMock

    import scripts.run_live as run_live_mod
    from src.adapters.indstocks_market_data import OptionChainResult, OptionLeg

    async def _inner():
        settings = run_live_mod.load_settings()
        mapper = InstrumentMapper("config/instruments.json")

        ce = OptionLeg(
            security_id="45108", trading_symbol="NIFTY-Aug2026-24450-CE", last_price=100.0,
            previous_close_price=90, oi=1, previous_oi=1, volume=1, top_bid_price=99,
            top_bid_quantity=1, top_ask_price=101, top_ask_quantity=1, iv=10,
            delta=0.5, gamma=0.001, theta=-1, vega=1,
        )
        pe = OptionLeg(
            security_id="45109", trading_symbol="NIFTY-Aug2026-24450-PE", last_price=80.0,
            previous_close_price=90, oi=1, previous_oi=1, volume=1, top_bid_price=79,
            top_bid_quantity=1, top_ask_price=81, top_ask_quantity=1, iv=10,
            delta=-0.5, gamma=0.001, theta=-1, vega=1,
        )
        chain = OptionChainResult(underlying_ltp=24471.7, expiry="2026-08-25", strikes={24450.0: (ce, pe)})

        fake_md = AsyncMock()
        fake_md.list_expiries.return_value = ["2026-08-25", "2026-09-01"]
        fake_md.option_chain.return_value = chain

        strategy = await run_live_mod.resolve_orpc_strategy(settings, mapper, fake_md, quantity=75)
        assert strategy._atm_ce_token == "NFO:45108"
        assert strategy._atm_pe_token == "NFO:45109"
        assert strategy._quantity == 75

    asyncio.run(_inner())


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    passed, failed = 0, 0
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {test.__name__} -> {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
