"""
strategies/vwap_reversion.py

Mean reversion: when price stretches beyond N standard deviations from
the session-anchored VWAP, bets on reversion back toward VWAP.
"""

from __future__ import annotations

from src.adapters.indstocks_data import RawTick
from src.core.signals import Direction, OpportunitySignal
from src.features.vwap_engine import AnchoredVWAP
from src.strategies.base import BaseStrategy


class VWAPReversionStrategy(BaseStrategy):
    strategy_id = "vwap_reversion_v1"

    def __init__(
        self,
        instrument_token: str,
        quantity: int,
        entry_std_dev: float = 2.0,
        min_ticks_before_entry: int = 100,
        confidence: float = 0.4,
    ):
        self._instrument_token = instrument_token
        self._quantity = quantity
        self._entry_std_dev = entry_std_dev
        self._min_ticks = min_ticks_before_entry
        self._confidence = confidence

        self._vwap = AnchoredVWAP()
        self._tick_count = 0
        self._fired_this_stretch = False

    def on_session_start(self) -> None:
        self._vwap.reset()
        self._tick_count = 0
        self._fired_this_stretch = False

    def on_tick(self, tick: RawTick) -> OpportunitySignal | None:
        if tick.indstocks_token != self._instrument_token:
            return None

        volume = tick.volume or 1
        self._vwap.update(tick.ltp, volume)
        self._tick_count += 1

        if self._tick_count < self._min_ticks:
            return None

        band = self._vwap.band(self._entry_std_dev)
        if band is None:
            return None
        lower, upper = band

        if lower <= tick.ltp <= upper:
            self._fired_this_stretch = False  # back inside band, re-arm
            return None

        if self._fired_this_stretch:
            return None  # already faded this stretch, wait for re-arm

        deviation_bps = self._vwap.deviation_bps(tick.ltp) or 0.0
        direction = Direction.SELL if tick.ltp > upper else Direction.BUY

        self._fired_this_stretch = True

        return OpportunitySignal(
            strategy_id=self.strategy_id,
            instrument_id=self._instrument_token,
            direction=direction,
            raw_alpha_bps=abs(deviation_bps) * 0.5,  # expect partial reversion
            confidence=self._confidence,
            estimated_duration_sec=120,
            quantity=self._quantity,
            metadata={
                "vwap": self._vwap.vwap,
                "deviation_bps": deviation_bps,
                "band_lower": lower,
                "band_upper": upper,
            },
        )
