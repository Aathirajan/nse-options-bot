"""
strategies/vol_breakout.py

Consolidation squeeze breakout: tracks a rolling price range; when a
tight-range period (low realized volatility) is followed by a decisive
break beyond that range, bets on continuation in the breakout direction.
"""

from __future__ import annotations

from collections import deque

from src.adapters.indstocks_data import RawTick
from src.core.signals import Direction, OpportunitySignal
from src.strategies.base import BaseStrategy


class VolBreakoutStrategy(BaseStrategy):
    strategy_id = "vol_breakout_v1"

    def __init__(
        self,
        instrument_token: str,
        quantity: int,
        lookback_ticks: int = 200,
        squeeze_range_bps_max: float = 30.0,  # range must be tighter than this to count as a squeeze
        breakout_buffer_bps: float = 5.0,     # must clear range by this much to trigger
        confidence: float = 0.45,
    ):
        self._instrument_token = instrument_token
        self._quantity = quantity
        self._lookback = lookback_ticks
        self._squeeze_max = squeeze_range_bps_max
        self._breakout_buffer = breakout_buffer_bps
        self._confidence = confidence

        self._prices: deque[float] = deque(maxlen=lookback_ticks)
        self._fired_this_breakout = False

    def on_session_start(self) -> None:
        self._prices.clear()
        self._fired_this_breakout = False

    def on_tick(self, tick: RawTick) -> OpportunitySignal | None:
        if tick.indstocks_token != self._instrument_token:
            return None

        if len(self._prices) < self._lookback:
            self._prices.append(tick.ltp)
            return None

        range_low = min(self._prices)
        range_high = max(self._prices)
        mid = (range_low + range_high) / 2
        range_bps = ((range_high - range_low) / mid) * 10_000 if mid else 0.0

        was_squeezed = range_bps <= self._squeeze_max
        self._prices.append(tick.ltp)

        if not was_squeezed:
            self._fired_this_breakout = False
            return None

        breakout_up = tick.ltp > range_high * (1 + self._breakout_buffer / 10_000)
        breakout_down = tick.ltp < range_low * (1 - self._breakout_buffer / 10_000)

        if not (breakout_up or breakout_down):
            return None
        if self._fired_this_breakout:
            return None

        self._fired_this_breakout = True
        direction = Direction.BUY if breakout_up else Direction.SELL

        return OpportunitySignal(
            strategy_id=self.strategy_id,
            instrument_id=self._instrument_token,
            direction=direction,
            raw_alpha_bps=range_bps * 1.5,  # expect a move roughly proportional to the squeeze
            confidence=self._confidence,
            estimated_duration_sec=90,
            quantity=self._quantity,
            metadata={"range_bps": range_bps, "range_low": range_low, "range_high": range_high},
        )
