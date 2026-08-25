"""
strategies/base.py

Every strategy implements this tiny interface: consume ticks, optionally
emit an OpportunitySignal. Strategies never place orders directly — they
only emit signals, which the Arbiter (core/arbiter.py) scores and gates.

This intentionally does NOT subclass a Nautilus Strategy base class here,
so the signal-generation logic can be unit tested without spinning up a
full Nautilus engine. The scripts/run_backtest.py and run_live.py
entrypoints are where this gets wrapped into an actual
nautilus_trader.trading.strategy.Strategy that receives real QuoteTick/
TradeTick events from the engine and forwards them into these methods.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.adapters.indstocks_data import RawTick
from src.core.signals import OpportunitySignal


class BaseStrategy(ABC):
    strategy_id: str

    @abstractmethod
    def on_tick(self, tick: RawTick) -> OpportunitySignal | None:
        """Process one tick; return a signal if an opportunity is found."""
        raise NotImplementedError

    def on_session_start(self) -> None:
        """Optional hook, called once at the start of each trading day."""
        pass
