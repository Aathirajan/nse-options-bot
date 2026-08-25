"""
strategies/nautilus_bridge.py

Wraps one of our existing BaseStrategy implementations (obi_momentum,
vwap_reversion, vol_breakout, opening_dislocation) inside a real
nautilus_trader Strategy, so it can run inside an actual BacktestEngine
and receive real QuoteTick events, with signals routed through our
Arbiter exactly as they are in scripts/run_backtest.py's simplified
replay loop.

This is the "graduate to full Nautilus" path referenced as a TODO in the
original scaffold — confirmed working against nautilus_trader==1.231.0
in tests/test_nautilus_integration.py.

Design note: our BaseStrategy classes were written to consume RawTick
(from adapters/indstocks_data.py), not Nautilus QuoteTick — rather than
rewriting every strategy's on_tick() signature, this bridge converts
each incoming QuoteTick into a RawTick on the fly. That keeps the same
strategy code working unmodified across the simplified backtest
(scripts/run_backtest.py), live trading (scripts/run_live.py), and now
the full Nautilus engine.
"""

from __future__ import annotations

import logging

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from src.adapters.indstocks_data import RawTick
from src.adapters.depth_poller import DepthPoller
from src.core.arbiter import OpportunityArbiter
from src.core.signals import Direction
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class OpportunityEngineStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: str  # Nautilus instrument id string, e.g. "NIFTY.INDEX"


class OpportunityEngineStrategy(Strategy):
    """
    Generic bridge: takes any BaseStrategy + our Arbiter, subscribes to
    quote ticks for one instrument, and submits real Nautilus orders for
    every approved signal. is_option should reflect whether this
    instrument_id is an options contract, for correct cost estimation.
    """

    def __init__(
        self,
        config: OpportunityEngineStrategyConfig,
        inner_strategy: BaseStrategy,
        arbiter: OpportunityArbiter,
        is_option: bool = False,
        additional_instrument_ids: tuple[str, ...] = (),
        on_halt=None,  # optional callable(arbiter, reason) -> awaitable, called when halt detected
        depth_poller: DepthPoller | None = None,
    ):
        super().__init__(config)
        self._inner = inner_strategy
        self._arbiter = arbiter
        self._is_option = is_option
        self._instrument_id = InstrumentId.from_str(config.instrument_id)
        self._subscription_ids = (self._instrument_id,) + tuple(
            InstrumentId.from_str(value) for value in additional_instrument_ids
        )
        self._on_halt = on_halt
        self._halt_triggered = False
        self._depth_poller = depth_poller

    def on_start(self) -> None:
        self._inner.on_session_start()
        for instrument_id in self._subscription_ids:
            self.subscribe_quote_ticks(instrument_id)
        logger.info("Subscribed to quote ticks for %s", self._subscription_ids)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        raw_tick = RawTick(
            ws_token=str(tick.instrument_id),
            ltp=float(tick.bid_price + tick.ask_price) / 2,
            timestamp_ns=tick.ts_event,
            bid=float(tick.bid_price),
            ask=float(tick.ask_price),
            bid_qty=int(tick.bid_size),
            ask_qty=int(tick.ask_size),
        )

        # Enrich with real depth data if available
        if self._depth_poller:
            depth = self._depth_poller.get_latest_depth(raw_tick.ws_token)
            if depth:
                raw_tick.bid = depth.bid_price
                raw_tick.ask = depth.ask_price
                raw_tick.bid_qty = depth.bid_qty
                raw_tick.ask_qty = depth.ask_qty

        signal = self._inner.on_tick(raw_tick)
        if signal is None:
            return

        # Use real polled spread if available
        spread_pct = 0.0
        if self._depth_poller:
            signal_depth = self._depth_poller.get_latest_depth(signal.instrument_id)
            if signal_depth:
                spread_pct = signal_depth.spread_pct
        if spread_pct == 0.0 and raw_tick.bid and raw_tick.ask and raw_tick.bid > 0:
            spread_pct = ((raw_tick.ask - raw_tick.bid) / raw_tick.bid) * 100

        decision = self._arbiter.evaluate_single(
            signal,
            notional_inr=raw_tick.ltp * signal.quantity,
            bid_ask_spread_pct=spread_pct,
            expected_slippage_pct=0.1,
            is_option=self._is_option,
        )

        if not decision.approved:
            self.log.info(
                f"Rejected {signal.strategy_id} {signal.direction.value}: "
                f"{decision.reason} (net_ev={decision.net_ev_bps:.1f}bps)"
            )
            # Check if arbiter was halted due to daily loss breach
            if not self._halt_triggered and self._arbiter.is_halted():
                self._halt_triggered = True
                reason = self._arbiter.get_halt_reason() or "daily loss limit breached"
                self.log.critical(f"Arbiter halted: {reason}")
                if self._on_halt:
                    # Schedule the async callback on the strategy's event loop
                    import asyncio
                    asyncio.create_task(self._on_halt(self._arbiter, reason))
            return

        self.log.info(
            f"APPROVED {signal.strategy_id} {signal.direction.value} "
            f"qty={signal.quantity} net_ev={decision.net_ev_bps:.1f}bps -> submitting order"
        )

        order_side = OrderSide.BUY if signal.direction == Direction.BUY else OrderSide.SELL
        instrument = self.cache.instrument(self._instrument_id)

        if signal.limit_price is not None:
            order = self.order_factory.limit(
                instrument_id=self._instrument_id,
                order_side=order_side,
                quantity=instrument.make_qty(signal.quantity),
                price=instrument.make_price(signal.limit_price),
                time_in_force=TimeInForce.DAY,
            )
        else:
            order = self.order_factory.market(
                instrument_id=self._instrument_id,
                order_side=order_side,
                quantity=instrument.make_qty(signal.quantity),
            )

        self.submit_order(order)

    def on_stop(self) -> None:
        for instrument_id in self._subscription_ids:
            self.unsubscribe_quote_ticks(instrument_id)
