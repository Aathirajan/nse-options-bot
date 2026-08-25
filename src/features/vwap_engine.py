"""
features/vwap_engine.py

Anchored VWAP (typically anchored to session open) with standard-deviation
bands, for mean-reversion and breakout strategies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class AnchoredVWAP:
    """
    Call reset() at the anchor point (e.g. 09:15:00 session open), then
    update() on every trade tick for the rest of the session.
    """

    _cum_pv: float = field(default=0.0, init=False)
    _cum_vol: float = field(default=0.0, init=False)
    _cum_pv2: float = field(default=0.0, init=False)  # for variance of price around vwap
    _n: int = field(default=0, init=False)

    def reset(self) -> None:
        self._cum_pv = 0.0
        self._cum_vol = 0.0
        self._cum_pv2 = 0.0
        self._n = 0

    def update(self, price: float, volume: int) -> None:
        if volume <= 0:
            return
        self._cum_pv += price * volume
        self._cum_vol += volume
        self._cum_pv2 += (price**2) * volume
        self._n += 1

    @property
    def vwap(self) -> float | None:
        if self._cum_vol == 0:
            return None
        return self._cum_pv / self._cum_vol

    @property
    def std_dev(self) -> float | None:
        """Volume-weighted standard deviation of price around VWAP."""
        vwap = self.vwap
        if vwap is None or self._cum_vol == 0:
            return None
        mean_sq = self._cum_pv2 / self._cum_vol
        variance = mean_sq - vwap**2
        return math.sqrt(max(variance, 0.0))

    def band(self, num_std: float) -> tuple[float, float] | None:
        """Returns (lower, upper) band at num_std standard deviations."""
        vwap = self.vwap
        std = self.std_dev
        if vwap is None or std is None:
            return None
        return (vwap - num_std * std, vwap + num_std * std)

    def deviation_bps(self, price: float) -> float | None:
        """How far `price` is from VWAP, in basis points."""
        vwap = self.vwap
        if vwap is None or vwap == 0:
            return None
        return ((price - vwap) / vwap) * 10_000
