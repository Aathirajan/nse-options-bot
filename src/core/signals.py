"""
core/signals.py

Standardized event that every strategy publishes instead of placing orders
directly. The Arbiter (arbiter.py) consumes these, scores them against
Indian market frictions (cost_engine.py), and decides which ones to route
to execution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class OpportunitySignal:
    """
    A candidate trade emitted by a strategy. This is NOT an order — it is a
    proposal that competes with signals from other strategies for capital.

    Attributes:
        strategy_id: identifies which strategy emitted this (e.g. "orpc_v1").
        instrument_id: Nautilus-format instrument id, e.g. "NIFTY26AUG24500CE-NFO".
        direction: BUY or SELL.
        raw_alpha_bps: expected move in basis points, before any costs.
        confidence: 0.0-1.0, e.g. derived from order book imbalance strength
            or historical hit-rate of this signal type.
        estimated_duration_sec: how long the strategy expects to hold, used
            by the arbiter to estimate time-decay / theta risk for options.
        quantity: proposed position size in lots/units. Strategies size this
            themselves; the arbiter may scale it down for risk but never up.
        limit_price: optional price cap for entry; None means "market-ish"
            (arbiter will apply a slippage buffer).
        metadata: free-form dict for strategy-specific context (e.g. the
            OBI reading, VWAP deviation, CE/PE gap) — useful for post-trade
            analysis, not used by the arbiter's core scoring.
        timestamp_ns: emission time in epoch nanoseconds.
    """

    strategy_id: str
    instrument_id: str
    direction: Direction
    raw_alpha_bps: float
    confidence: float
    estimated_duration_sec: int
    quantity: int
    limit_price: float | None = None
    metadata: dict = field(default_factory=dict)
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())

    def __post_init__(self):
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")


@dataclass(frozen=True)
class ArbiterDecision:
    """Result of the Arbiter scoring a signal."""

    signal: OpportunitySignal
    net_ev_bps: float
    approved: bool
    reason: str
    estimated_cost_bps: float
