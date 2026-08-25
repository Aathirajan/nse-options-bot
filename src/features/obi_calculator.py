"""
features/obi_calculator.py

Order Book Imbalance (OBI): measures buy vs sell pressure from top-of-book
(or multi-level) quantities. Positive OBI = more bid interest (bullish
pressure), negative = more ask interest (bearish pressure).

    OBI = (bid_qty - ask_qty) / (bid_qty + ask_qty)   -> range [-1, 1]

For multi-level depth, a weighted variant discounts levels further from
the touch, since resting size deep in the book is less predictive of an
imminent move than size at the best bid/ask.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class DepthLevel:
    price: float
    quantity: int


def top_of_book_obi(bid_qty: int, ask_qty: int) -> float:
    total = bid_qty + ask_qty
    if total == 0:
        return 0.0
    return (bid_qty - ask_qty) / total


def weighted_depth_obi(
    bid_levels: list[DepthLevel],
    ask_levels: list[DepthLevel],
    decay: float = 0.7,
) -> float:
    """
    Weights level i (0-indexed from touch) by decay**i, so the best bid/ask
    matters most and deeper resting size matters progressively less.
    """
    weighted_bid = sum(lvl.quantity * (decay**i) for i, lvl in enumerate(bid_levels))
    weighted_ask = sum(lvl.quantity * (decay**i) for i, lvl in enumerate(ask_levels))
    total = weighted_bid + weighted_ask
    if total == 0:
        return 0.0
    return (weighted_bid - weighted_ask) / total


class OBIStream:
    """
    Rolling OBI tracker for a single instrument — smooths tick-level noise
    with a short moving average and exposes both the instantaneous and
    smoothed reading, which strategies can use to require the smoothed
    value to confirm a fast instantaneous spike (reduces false triggers
    from a single large resting order appearing/disappearing).
    """

    def __init__(self, window: int = 20):
        self._window = window
        self._history: deque[float] = deque(maxlen=window)
        self.last_obi: float = 0.0

    def update(self, bid_qty: int, ask_qty: int) -> float:
        obi = top_of_book_obi(bid_qty, ask_qty)
        self._history.append(obi)
        self.last_obi = obi
        return obi

    @property
    def smoothed_obi(self) -> float:
        if not self._history:
            return 0.0
        return sum(self._history) / len(self._history)
