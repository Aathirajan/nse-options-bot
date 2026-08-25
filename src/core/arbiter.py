"""
core/arbiter.py

The Opportunity Arbiter sits between strategies and execution. Every
strategy emits OpportunitySignal events instead of placing orders; the
Arbiter:

  1. Scores each signal's Net EV using CostEngine (real Indian frictions).
  2. Filters out anything below the configured threshold.
  3. Ranks surviving signals when multiple compete for capital at once.
  4. Enforces basic risk limits (max concurrent positions, daily loss cap,
     max position size) before approving.

This module has NO network/broker dependency — it's pure decision logic,
which makes it the easiest piece to unit test and to reuse identically
across BACKTEST, PAPER, and LIVE modes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from src.core.cost_engine import CostEngine
from src.core.signals import ArbiterDecision, Direction, OpportunitySignal


@dataclass
class RiskLimits:
    net_ev_threshold_bps: float = 15.0
    max_concurrent_positions: int = 3
    max_daily_loss_inr: float = 5000.0
    max_position_size_units: int = 150


@dataclass
class ArbiterState:
    """Mutable state the Arbiter tracks across a trading session."""

    open_positions: int = 0
    realized_pnl_today_inr: float = 0.0
    approved_today: list = field(default_factory=list)
    pending_orders: dict[str, "PendingOrderState"] = field(default_factory=dict)
    pending_slots: int = 0
    positions: dict[str, "PositionState"] = field(default_factory=dict)
    halted: bool = False
    halt_reason: str | None = None

    @property
    def realized_pnl(self) -> float:
        return self.realized_pnl_today_inr


class OrderLifecycle(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


@dataclass
class PendingOrderState:
    signal: OpportunitySignal
    entry_price: float
    filled_quantity: int = 0
    lifecycle: OrderLifecycle = OrderLifecycle.PENDING


@dataclass
class PositionState:
    instrument_id: str
    quantity: int
    direction: Direction
    average_entry_price: float


class OpportunityArbiter:
    def __init__(
        self,
        cost_engine: CostEngine,
        risk_limits: RiskLimits | None = None,
    ):
        self.cost_engine = cost_engine
        self.risk_limits = risk_limits or RiskLimits()
        self.state = ArbiterState()

    def score(
        self,
        signal: OpportunitySignal,
        *,
        notional_inr: float,
        bid_ask_spread_pct: float,
        expected_slippage_pct: float,
        is_option: bool,
    ) -> ArbiterDecision:
        """Score a single signal without applying risk-limit gating."""
        cost_bps = self.cost_engine.estimate_round_trip_cost_bps(
            entry_direction=signal.direction,
            notional_inr=notional_inr,
            bid_ask_spread_pct=bid_ask_spread_pct,
            expected_slippage_pct=expected_slippage_pct,
            is_option=is_option,
        )
        raw_ev_bps = signal.raw_alpha_bps * signal.confidence
        net_ev_bps = raw_ev_bps - cost_bps

        approved = net_ev_bps > self.risk_limits.net_ev_threshold_bps
        reason = (
            "net EV exceeds threshold"
            if approved
            else f"net EV {net_ev_bps:.1f}bps below threshold "
            f"{self.risk_limits.net_ev_threshold_bps:.1f}bps"
        )

        return ArbiterDecision(
            signal=signal,
            net_ev_bps=net_ev_bps,
            approved=approved,
            reason=reason,
            estimated_cost_bps=cost_bps,
        )

    def evaluate_single(
        self,
        signal: OpportunitySignal,
        *,
        notional_inr: float,
        bid_ask_spread_pct: float,
        expected_slippage_pct: float,
        is_option: bool,
    ) -> ArbiterDecision:
        """
        Score AND gate a single signal against current risk state, then
        update self.state if approved — the entry point live/backtest
        callers processing one tick at a time should use (run_live.py,
        the Nautilus strategy bridge), as opposed to score() (pure,
        no state mutation — used by evaluate_batch for ranking multiple
        simultaneous candidates before any of them touch position state).
        """
        if self.state.halted:
            return ArbiterDecision(
                signal=signal,
                net_ev_bps=0.0,
                approved=False,
                reason=f"TRADING HALTED — {self.state.halt_reason or 'daily loss limit breached, manual restart required'}",
                estimated_cost_bps=0.0,
            )

        decision = self.score(
            signal,
            notional_inr=notional_inr,
            bid_ask_spread_pct=bid_ask_spread_pct,
            expected_slippage_pct=expected_slippage_pct,
            is_option=is_option,
        )
        if not decision.approved:
            return decision

        ok, reason = self._passes_risk_limits(signal)
        if not ok:
            return ArbiterDecision(
                signal=decision.signal,
                net_ev_bps=decision.net_ev_bps,
                approved=False,
                reason=reason,
                estimated_cost_bps=decision.estimated_cost_bps,
            )

        self.state.pending_slots += 1
        self.state.approved_today.append(decision)
        return decision

    def _passes_risk_limits(self, signal: OpportunitySignal) -> tuple[bool, str]:
        position = self.state.positions.get(signal.instrument_id)
        is_closing = position is not None and position.direction != signal.direction
        if not is_closing and self.state.open_positions + self.state.pending_slots >= self.risk_limits.max_concurrent_positions:
            return False, "max concurrent positions reached"
        if self.state.realized_pnl_today_inr <= -self.risk_limits.max_daily_loss_inr:
            if not self.state.halted:
                self.halt("daily loss limit breached — trading halted, manual restart required")
            return False, "daily loss limit hit — trading halted"
        if signal.quantity > self.risk_limits.max_position_size_units:
            return False, "position size exceeds max allowed"
        return True, "ok"

    def evaluate_batch(
        self,
        scored: list[ArbiterDecision],
    ) -> list[ArbiterDecision]:
        """
        Given multiple already-scored signals competing for capital at the
        same moment, rank by net EV and apply risk-limit gating in order,
        so the best opportunities get first claim on the position slots.
        """
        if self.state.halted:
            halt_reason = self.state.halt_reason or "daily loss limit breached, manual restart required"
            return [
                ArbiterDecision(
                    signal=decision.signal,
                    net_ev_bps=0.0,
                    approved=False,
                    reason=f"TRADING HALTED — {halt_reason}",
                    estimated_cost_bps=0.0,
                )
                for decision in scored
            ]

        # Check daily loss limit before processing batch — halt on first breach
        if self.state.realized_pnl_today_inr <= -self.risk_limits.max_daily_loss_inr:
            self.halt("daily loss limit breached — trading halted, manual restart required")
            halt_reason = self.state.halt_reason or "daily loss limit breached, manual restart required"
            return [
                ArbiterDecision(
                    signal=decision.signal,
                    net_ev_bps=0.0,
                    approved=False,
                    reason=f"TRADING HALTED — {halt_reason}",
                    estimated_cost_bps=0.0,
                )
                for decision in scored
            ]

        ranked = sorted(scored, key=lambda d: d.net_ev_bps, reverse=True)
        final: list[ArbiterDecision] = []

        for decision in ranked:
            if not decision.approved:
                final.append(decision)
                continue

            ok, reason = self._passes_risk_limits(decision.signal)
            if ok:
                self.state.pending_slots += 1
                self.state.approved_today.append(decision)
                final.append(decision)
            else:
                final.append(
                    ArbiterDecision(
                        signal=decision.signal,
                        net_ev_bps=decision.net_ev_bps,
                        approved=False,
                        reason=reason,
                        estimated_cost_bps=decision.estimated_cost_bps,
                    )
                )

        return final

    def on_position_closed(self, pnl_inr: float) -> None:
        """Call this from the execution layer when a position closes out."""
        self.state.open_positions = max(0, self.state.open_positions - 1)
        self.state.realized_pnl_today_inr += pnl_inr

    def register_order(self, order_id: str, decision: ArbiterDecision, entry_price: float) -> None:
        if not decision.approved:
            raise ValueError("cannot register a rejected decision")
        self.state.pending_slots = max(0, self.state.pending_slots - 1)
        self.state.pending_orders[order_id] = PendingOrderState(
            signal=decision.signal, entry_price=entry_price
        )

    def handle_fill(self, update) -> OrderLifecycle | None:
        """Apply a broker update, including cumulative partial fills."""
        order = self.state.pending_orders.get(update.order_id)
        if order is None:
            return None
        status = str(update.order_status).upper()
        if status in {"REJECTED", "FAILED", "ABORTED", "EXPIRED"}:
            order.lifecycle = OrderLifecycle.REJECTED
            self.state.pending_orders.pop(update.order_id, None)
            return order.lifecycle
        if status in {"CANCELLED", "CANCELED"} and update.filled_quantity == 0:
            order.lifecycle = OrderLifecycle.CANCELLED
            self.state.pending_orders.pop(update.order_id, None)
            return order.lifecycle

        filled = int(update.filled_quantity)
        delta = filled - order.filled_quantity
        if delta <= 0:
            if status in {"CANCELLED", "CANCELED"}:
                order.lifecycle = OrderLifecycle.CANCELLED
                self.state.pending_orders.pop(update.order_id, None)
            return order.lifecycle
        order.filled_quantity = filled
        position = self.state.positions.get(order.signal.instrument_id)
        if position is None or position.direction == order.signal.direction:
            if position is None:
                self.state.positions[order.signal.instrument_id] = PositionState(
                    order.signal.instrument_id, filled, order.signal.direction,
                    float(update.average_price),
                )
                self.state.open_positions += 1
            else:
                total = position.quantity + delta
                position.average_entry_price = (
                    position.average_entry_price * position.quantity
                    + float(update.average_price) * delta
                ) / total
                position.quantity = total
            order.lifecycle = OrderLifecycle.OPEN
        else:
            close_quantity = min(delta, position.quantity)
            multiplier = 1 if position.direction == Direction.BUY else -1
            self.state.realized_pnl_today_inr += (
                float(update.average_price) - position.average_entry_price
            ) * close_quantity * multiplier
            position.quantity -= close_quantity
            order.lifecycle = OrderLifecycle.CLOSED if position.quantity == 0 else OrderLifecycle.OPEN
            if position.quantity == 0:
                self.state.positions.pop(order.signal.instrument_id)
                self.state.open_positions = max(0, self.state.open_positions - 1)

        if status in {"CANCELLED", "CANCELED"}:
            order.lifecycle = OrderLifecycle.CANCELLED
            self.state.pending_orders.pop(update.order_id, None)
        elif update.remaining_quantity == 0:
            self.state.pending_orders.pop(update.order_id, None)
        elif filled < int(update.filled_quantity) or update.remaining_quantity > 0:
            order.lifecycle = OrderLifecycle.PARTIALLY_FILLED
        return order.lifecycle

    def reconcile_open_positions(self, actual_open_positions: int, actual_realized_pnl: float | None = None) -> bool:
        positions_match = self.state.open_positions == actual_open_positions
        pnl_match = (
            actual_realized_pnl is None
            or abs(self.state.realized_pnl_today_inr - actual_realized_pnl) < 0.01
        )
        return positions_match and pnl_match

    def halt(self, reason: str) -> None:
        """Set the halted flag with a reason. Once halted, all signals are rejected."""
        self.state.halted = True
        self.state.halt_reason = reason

    def is_halted(self) -> bool:
        return self.state.halted

    def get_halt_reason(self) -> str | None:
        return self.state.halt_reason

    def get_open_positions_for_flattening(self) -> list[PositionState]:
        """Return current open positions that would need to be flattened on halt."""
        return list(self.state.positions.values())

    def get_pending_order_ids(self) -> list[str]:
        """Return pending order IDs that would need to be cancelled on halt."""
        return list(self.state.pending_orders.keys())

    def initialize_from_broker(
        self, open_positions: int, realized_pnl: float, positions: list[PositionState]
    ) -> None:
        """Initialize arbiter state from broker reconciliation data."""
        self.state.open_positions = open_positions
        self.state.realized_pnl_today_inr = realized_pnl
        self.state.positions = {pos.instrument_id: pos for pos in positions}

    def reset_daily_state(self) -> None:
        """Call at the start of each trading day."""
        self.state = ArbiterState()
