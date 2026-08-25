from __future__ import annotations

import asyncio

from src.adapters.indstocks_exec import IndstocksExecutionClient, OrderUpdate
from src.core.arbiter import OpportunityArbiter, OrderLifecycle, PositionState, RiskLimits
from src.core.cost_engine import CostConfig, CostEngine
from src.core.signals import Direction, OpportunitySignal


def _arbiter() -> OpportunityArbiter:
    return OpportunityArbiter(
        CostEngine(CostConfig()),
        RiskLimits(net_ev_threshold_bps=1.0, max_concurrent_positions=1, max_daily_loss_inr=5000.0),
    )


def _signal(direction: Direction) -> OpportunitySignal:
    return OpportunitySignal(
        strategy_id="test",
        instrument_id="NIFTY26AUG24000CE-NFO",
        direction=direction,
        raw_alpha_bps=500.0,
        confidence=0.9,
        estimated_duration_sec=60,
        quantity=10,
    )


def test_halt_flag_rejects_all_subsequent_signals():
    """Test that once halted, all signals are rejected with halt reason."""
    arbiter = _arbiter()
    
    # Normal signal should be approved
    decision = arbiter.evaluate_single(
        _signal(Direction.BUY), notional_inr=10000,
        bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True,
    )
    assert decision.approved
    
    # Halt the arbiter
    arbiter.halt("daily loss limit breached — trading halted, manual restart required")
    
    # All subsequent signals should be rejected
    decision = arbiter.evaluate_single(
        _signal(Direction.BUY), notional_inr=10000,
        bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True,
    )
    assert not decision.approved
    assert "TRADING HALTED" in decision.reason
    assert "daily loss limit breached" in decision.reason
    
    # evaluate_batch should also reject all
    decisions = arbiter.evaluate_batch([
        arbiter.score(_signal(Direction.BUY), notional_inr=10000, bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True),
        arbiter.score(_signal(Direction.SELL), notional_inr=10000, bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True),
    ])
    assert all(not d.approved for d in decisions)
    assert all("TRADING HALTED" in d.reason for d in decisions)
    
    # is_halted and get_halt_reason should work
    assert arbiter.is_halted()
    assert arbiter.get_halt_reason() == "daily loss limit breached — trading halted, manual restart required"


def test_daily_loss_breach_triggers_halt():
    """Test that exceeding max_daily_loss_inr triggers halt on first breach."""
    arbiter = _arbiter()
    
    # Simulate a large loss that exceeds the daily limit
    arbiter.state.realized_pnl_today_inr = -6000.0  # exceeds -5000 limit
    
    # Next signal should trigger halt and be rejected
    decision = arbiter.evaluate_single(
        _signal(Direction.BUY), notional_inr=10000,
        bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True,
    )
    assert not decision.approved
    assert "daily loss limit hit" in decision.reason
    assert arbiter.is_halted()
    
    # Subsequent signals should also be rejected with halt reason
    decision2 = arbiter.evaluate_single(
        _signal(Direction.BUY), notional_inr=10000,
        bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True,
    )
    assert not decision2.approved
    assert "TRADING HALTED" in decision2.reason


def test_daily_loss_breach_in_evaluate_batch_triggers_halt():
    """Test that daily loss breach in evaluate_batch triggers halt."""
    arbiter = _arbiter()
    arbiter.state.realized_pnl_today_inr = -6000.0
    
    scored = [
        arbiter.score(_signal(Direction.BUY), notional_inr=10000, bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True),
        arbiter.score(_signal(Direction.SELL), notional_inr=10000, bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True),
    ]
    decisions = arbiter.evaluate_batch(scored)
    
    assert all(not d.approved for d in decisions)
    assert all("TRADING HALTED" in d.reason for d in decisions)
    assert arbiter.is_halted()


def test_get_open_positions_for_flattening():
    """Test that get_open_positions_for_flattening returns current positions."""
    arbiter = _arbiter()
    
    # Add a position manually
    pos = PositionState("TEST-INSTRUMENT", 10, Direction.BUY, 100.0)
    arbiter.state.positions["TEST-INSTRUMENT"] = pos
    arbiter.state.open_positions = 1
    
    positions = arbiter.get_open_positions_for_flattening()
    assert len(positions) == 1
    assert positions[0].instrument_id == "TEST-INSTRUMENT"
    assert positions[0].quantity == 10
    assert positions[0].direction == Direction.BUY


def test_get_pending_order_ids():
    """Test that get_pending_order_ids returns pending order IDs."""
    arbiter = _arbiter()
    
    decision = arbiter.evaluate_single(
        _signal(Direction.BUY), notional_inr=10000,
        bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True,
    )
    arbiter.register_order("ORDER-123", decision, 100.0)
    
    order_ids = arbiter.get_pending_order_ids()
    assert "ORDER-123" in order_ids


def test_initialize_from_broker():
    """Test that initialize_from_broker correctly sets arbiter state."""
    arbiter = _arbiter()
    
    positions = [
        PositionState("INST-1", 10, Direction.BUY, 100.0),
        PositionState("INST-2", 5, Direction.SELL, 200.0),
    ]
    arbiter.initialize_from_broker(2, -100.5, positions)
    
    assert arbiter.state.open_positions == 2
    assert arbiter.state.realized_pnl_today_inr == -100.5
    assert len(arbiter.state.positions) == 2
    assert arbiter.state.positions["INST-1"].quantity == 10
    assert arbiter.state.positions["INST-1"].direction == Direction.BUY
    assert arbiter.state.positions["INST-2"].quantity == 5
    assert arbiter.state.positions["INST-2"].direction == Direction.SELL


def test_order_update_channel_opens_closes_and_releases_slot():
    async def run_test():
        arbiter = _arbiter()
        execution = IndstocksExecutionClient("FAKE")
        async def on_update(update):
            arbiter.handle_fill(update)
        execution.add_order_update_handler(on_update)

        entry = arbiter.evaluate_single(
            _signal(Direction.BUY), notional_inr=10000,
            bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True,
        )
        assert entry.approved
        assert arbiter.state.open_positions == 0
        arbiter.register_order("ENTRY-1", entry, 100.0)
        await execution._update_handlers[0](OrderUpdate(
            "ENTRY-1", "PARTIALLY_EXECUTED", 5, 5, 100.0, 1,
        ))
        assert arbiter.state.open_positions == 1
        assert arbiter.state.positions[entry.signal.instrument_id].quantity == 5

        await execution._update_handlers[0](OrderUpdate(
            "ENTRY-1", "SUCCESS", 10, 0, 101.0, 2,
        ))
        assert arbiter.state.positions[entry.signal.instrument_id].quantity == 10

        close = arbiter.evaluate_single(
            _signal(Direction.SELL), notional_inr=11000,
            bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True,
        )
        assert close.approved
        arbiter.register_order("EXIT-1", close, 101.0)
        lifecycle = arbiter.handle_fill(OrderUpdate("EXIT-1", "SUCCESS", 10, 0, 110.0, 3))
        assert lifecycle is OrderLifecycle.CLOSED
        assert arbiter.state.open_positions == 0
        assert arbiter.state.realized_pnl_today_inr == 95.0

        reusable = arbiter.evaluate_single(
            _signal(Direction.BUY), notional_inr=10000,
            bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True,
        )
        assert reusable.approved

        await execution.close()

    asyncio.run(run_test())


def test_rejected_order_releases_pending_slot():
    arbiter = _arbiter()
    decision = arbiter.evaluate_single(
        _signal(Direction.BUY), notional_inr=10000,
        bid_ask_spread_pct=0.1, expected_slippage_pct=0.1, is_option=True,
    )
    arbiter.register_order("REJECTED-1", decision, 100.0)
    lifecycle = arbiter.handle_fill(OrderUpdate("REJECTED-1", "FAILED", 0, 10, 0, 1))
    assert lifecycle is OrderLifecycle.REJECTED
    assert not arbiter.state.pending_orders
    assert arbiter.state.open_positions == 0
