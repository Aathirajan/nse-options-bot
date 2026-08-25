"""
strategies/opening_dislocation.py

Opening Range Premium Convergence (ORPC) — the strategy from your
orpc-bot project, re-expressed as a signal-emitting module inside the
opportunity engine instead of a standalone trading loop.

At exactly the session open (09:15:00 IST), compares the ATM call and
put premiums; buys whichever is cheaper, betting the CE/PE premium gap
converges. Emits at most one signal per session, at the open.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from src.adapters.indstocks_data import RawTick
from src.core.signals import Direction, OpportunitySignal
from src.strategies.base import BaseStrategy

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SESSION_OPEN = dt.time(9, 15, 0)


class OpeningRangePremiumConvergence(BaseStrategy):
    strategy_id = "orpc_v1"

    def __init__(
        self,
        atm_ce_token: str,
        atm_pe_token: str,
        quantity: int,
        entry_slippage_buffer_pct: float = 10.0,
        confidence: float = 0.55,
    ):
        self._atm_ce_token = atm_ce_token
        self._atm_pe_token = atm_pe_token
        self._quantity = quantity
        self._entry_slippage_buffer_pct = entry_slippage_buffer_pct
        self._confidence = confidence

        self._ce_price: float | None = None
        self._pe_price: float | None = None
        self._fired_today = False

    def on_session_start(self) -> None:
        self._ce_price = None
        self._pe_price = None
        self._fired_today = False

    def on_tick(self, tick: RawTick) -> OpportunitySignal | None:
        if self._fired_today:
            return None

        if tick.indstocks_token == self._atm_ce_token:
            self._ce_price = tick.ltp
        elif tick.indstocks_token == self._atm_pe_token:
            self._pe_price = tick.ltp
        else:
            return None

        if self._ce_price is None or self._pe_price is None:
            return None

        tick_time = dt.datetime.fromtimestamp(
            tick.timestamp_ns / 1e9, tz=IST
        ).time()
        if tick_time < SESSION_OPEN:
            return None  # wait for the actual session open tick

        cheaper_token, cheaper_price = (
            (self._atm_ce_token, self._ce_price)
            if self._ce_price < self._pe_price
            else (self._atm_pe_token, self._pe_price)
        )
        gap_pct = abs(self._ce_price - self._pe_price) / cheaper_price * 100

        self._fired_today = True

        limit_price = cheaper_price * (1 + self._entry_slippage_buffer_pct / 100)

        return OpportunitySignal(
            strategy_id=self.strategy_id,
            instrument_id=cheaper_token,
            direction=Direction.BUY,
            raw_alpha_bps=gap_pct * 100,  # convert % gap -> bps, pre-confidence
            confidence=self._confidence,
            estimated_duration_sec=180,  # convergence expected within minutes
            quantity=self._quantity,
            limit_price=round(limit_price, 2),
            metadata={
                "ce_price": self._ce_price,
                "pe_price": self._pe_price,
                "gap_pct": gap_pct,
            },
        )


class DynamicOpeningRangePremiumConvergence(OpeningRangePremiumConvergence):
    """Refresh the ATM CE/PE pair once per IST trading date."""

    def __init__(
        self,
        resolver: Callable[[dt.date], tuple[str, str]],
        quantity: int,
        entry_slippage_buffer_pct: float = 10.0,
        confidence: float = 0.55,
    ):
        self._resolver = resolver
        self._resolved_date: dt.date | None = None
        super().__init__("", "", quantity, entry_slippage_buffer_pct, confidence)

    def _refresh(self, session_date: dt.date) -> None:
        self._atm_ce_token, self._atm_pe_token = self._resolver(session_date)
        self._resolved_date = session_date
        super().on_session_start()

    def on_session_start(self) -> None:
        today = dt.datetime.now(tz=IST).date()
        if self._resolved_date != today:
            self._refresh(today)
        else:
            super().on_session_start()

    def on_tick(self, tick: RawTick) -> OpportunitySignal | None:
        session_date = dt.datetime.fromtimestamp(
            tick.timestamp_ns / 1e9, tz=IST
        ).date()
        if self._resolved_date != session_date:
            self._refresh(session_date)
        return super().on_tick(tick)
