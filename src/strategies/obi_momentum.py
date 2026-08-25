"""
strategies/obi_momentum.py

Order Book Imbalance momentum: when smoothed OBI crosses a strong
directional threshold, bets on short-term continuation in that direction.
Requires the instantaneous reading to confirm the smoothed trend (reduces
false triggers from a single large order appearing/disappearing).
"""

from __future__ import annotations

from src.adapters.indstocks_data import RawTick
from src.core.signals import Direction, OpportunitySignal
from src.features.obi_calculator import OBIStream
from src.strategies.base import BaseStrategy


class OBIMomentumStrategy(BaseStrategy):
    strategy_id = "obi_momentum_v1"

    def __init__(
        self,
        instrument_token: str,
        quantity: int,
        obi_entry_threshold: float = 0.4,
        obi_window: int = 20,
        cooldown_ticks: int = 50,
        confidence: float = 0.45,
    ):
        self._instrument_token = instrument_token
        self._quantity = quantity
        self._threshold = obi_entry_threshold
        self._cooldown_ticks = cooldown_ticks
        self._confidence = confidence

        self._obi = OBIStream(window=obi_window)
        self._ticks_since_signal = cooldown_ticks  # allow immediate first signal

    def on_session_start(self) -> None:
        self._obi = OBIStream(window=self._obi._window)
        self._ticks_since_signal = self._cooldown_ticks

    def on_tick(self, tick: RawTick) -> OpportunitySignal | None:
        if tick.indstocks_token != self._instrument_token:
            return None
        if tick.bid_qty is None or tick.ask_qty is None:
            return None

        instantaneous = self._obi.update(tick.bid_qty, tick.ask_qty)
        smoothed = self._obi.smoothed_obi
        self._ticks_since_signal += 1

        if self._ticks_since_signal < self._cooldown_ticks:
            return None

        # Require both instantaneous and smoothed readings to agree on
        # direction and clear the threshold — filters single-order noise.
        strong_buy = instantaneous > self._threshold and smoothed > self._threshold * 0.6
        strong_sell = instantaneous < -self._threshold and smoothed < -self._threshold * 0.6

        if not (strong_buy or strong_sell):
            return None

        self._ticks_since_signal = 0
        direction = Direction.BUY if strong_buy else Direction.SELL
        strength = abs(smoothed)

        return OpportunitySignal(
            strategy_id=self.strategy_id,
            instrument_id=self._instrument_token,
            direction=direction,
            raw_alpha_bps=strength * 60,  # heuristic scaling, calibrate via backtest
            confidence=min(self._confidence + strength * 0.2, 0.9),
            estimated_duration_sec=20,
            quantity=self._quantity,
            metadata={"instantaneous_obi": instantaneous, "smoothed_obi": smoothed},
        )
