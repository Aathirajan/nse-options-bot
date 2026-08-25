"""
adapters/nautilus_live_exec_client.py

Wraps IndstocksExecutionClient (indstocks_exec.py) inside a real
nautilus_trader LiveExecutionClient.

Overridden methods and their exact async signatures below are confirmed
by direct introspection of nautilus_trader==1.231.0's
LiveExecutionClient — not guessed:

    async def _connect(self) -> None
    async def _disconnect(self) -> None
    async def _submit_order(self, command: SubmitOrder) -> None
    async def _cancel_order(self, command: CancelOrder) -> None
    async def _modify_order(self, command: ModifyOrder) -> None

Fill/status events are reported back to the engine via the base
ExecutionClient's generate_order_* helpers (generate_order_submitted,
generate_order_accepted, generate_order_filled, generate_order_rejected,
generate_order_canceled) — confirmed signatures via introspection.

Known gap (see README/OrderUpdate docstring in indstocks_exec.py): the
WS order-update feed's status vocabulary ("PARTIALLY_EXECUTED") doesn't
obviously match the REST order-book's vocabulary ("PARTIALLY FILLED").
_map_order_update() below handles the statuses that ARE confirmed
(INITIATED/SUCCESS/CANCELLED/REJECTED-equivalents) and logs+ignores
anything unrecognized rather than guessing — extend the mapping once
you've captured real WS traffic and confirmed the full vocabulary.
"""

from __future__ import annotations

import asyncio

from nautilus_trader.execution.messages import CancelOrder, ModifyOrder, SubmitOrder
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.currencies import INR
from nautilus_trader.model.enums import LiquiditySide, OrderSide, OrderType
from nautilus_trader.model.objects import Money, Price, Quantity

from src.adapters.indstocks_exec import IndstocksExecutionClient, OrderRequest, OrderUpdate
from src.adapters.instrument_builder import find_record_by_real_instrument_id
from src.adapters.mapper import InstrumentMapper


class IndstocksLiveExecutionClient(LiveExecutionClient):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id,
        venue,
        oms_type,
        account_type,
        base_currency,
        instrument_provider,
        msgbus,
        cache,
        clock,
        access_token: str,
        mapper: InstrumentMapper,
        rest_base_url: str = "https://api.indstocks.com",
        ws_orders_url: str = "wss://ws-order-updates.indstocks.com/api/v1/ws/trades",
        dry_run: bool = True,
        config=None,
    ):
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=venue,
            oms_type=oms_type,
            account_type=account_type,
            base_currency=base_currency,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._mapper = mapper
        self._dry_run = dry_run
        self._client = IndstocksExecutionClient(
            access_token=access_token, rest_base_url=rest_base_url, ws_orders_url=ws_orders_url
        )
        self._client.add_order_update_handler(self._on_order_update)
        self._updates_task: asyncio.Task | None = None
        # our order_id (from IndstocksExecutionClient) -> the Nautilus
        # client_order_id/strategy_id/instrument_id we submitted it under,
        # needed to translate order updates back into the right events.
        self._order_context: dict[str, dict] = {}

    async def _connect(self) -> None:
        # A live execution client must register an account before it can
        # generate order events — mirrors the pattern real venue adapters
        # use (see nautilus_trader's own venue adapter implementations).
        # INDstocks doesn't expose a distinct "account id" concept beyond
        # your single trading account, so we synthesize one from the venue.
        from nautilus_trader.model.identifiers import AccountId

        account_id = AccountId(f"{self.id.value}-001")
        self._set_account_id(account_id)
        self.generate_account_state(
            balances=[],
            margins=[],
            reported=True,
            ts_event=self._clock.timestamp_ns(),
        )

        self._updates_task = self._loop.create_task(self._client.run_order_updates_forever())
        await asyncio.sleep(0)

    async def _disconnect(self) -> None:
        self._client.stop()
        if self._updates_task is not None:
            self._updates_task.cancel()
            try:
                await self._updates_task
            except asyncio.CancelledError:
                pass
        await self._client.close()

    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order
        record = find_record_by_real_instrument_id(self._mapper, order.instrument_id)
        if record is None:
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=f"No instrument mapping for {order.instrument_id}",
                ts_event=self._clock.timestamp_ns(),
            )
            return

        is_option = record.instrument_type == "OPTION"
        request = OrderRequest(
            security_id=record.security_id,
            exchange=record.exchange,
            segment="DERIVATIVE" if is_option else "EQUITY",
            side=order.side.name,  # OrderSide.BUY/SELL -> "BUY"/"SELL"
            quantity=int(order.quantity),
            order_type="LIMIT" if order.order_type == OrderType.LIMIT else "MARKET",
            limit_price=float(order.price) if order.has_price else None,
            remarks=f"{command.strategy_id}/{order.client_order_id}"[:100],
        )

        self.generate_order_submitted(
            strategy_id=command.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

        try:
            result = await self._client.place_order(request, dry_run=self._dry_run)
        except Exception as exc:  # noqa: BLE001 — report as a rejection, don't crash the client
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=str(exc),
                ts_event=self._clock.timestamp_ns(),
            )
            return

        self._order_context[result.order_id] = {
            "strategy_id": command.strategy_id,
            "instrument_id": order.instrument_id,
            "client_order_id": order.client_order_id,
            "side": order.side,
            "order_type": order.order_type,
        }

        if result.order_status in ("SUCCESS", "INITIATED", "DRY_RUN"):
            self.generate_order_accepted(
                strategy_id=command.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=self._venue_order_id(result.order_id),
                ts_event=self._clock.timestamp_ns(),
            )
        elif result.order_status in ("REJECTED", "FAILED"):
            self.generate_order_rejected(
                strategy_id=command.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=f"INDstocks order_status={result.order_status}",
                ts_event=self._clock.timestamp_ns(),
            )

    async def _cancel_order(self, command: CancelOrder) -> None:
        record = find_record_by_real_instrument_id(self._mapper, command.instrument_id)
        segment = "DERIVATIVE" if record and record.instrument_type == "OPTION" else "EQUITY"
        await self._client.cancel_order(
            order_id=str(command.venue_order_id), segment=segment, dry_run=self._dry_run
        )

    async def _modify_order(self, command: ModifyOrder) -> None:
        record = find_record_by_real_instrument_id(self._mapper, command.instrument_id)
        segment = "DERIVATIVE" if record and record.instrument_type == "OPTION" else "EQUITY"
        await self._client.modify_order(
            order_id=str(command.venue_order_id),
            segment=segment,
            qty=int(command.quantity) if command.quantity is not None else 0,
            limit_price=float(command.price) if command.price is not None else 0.0,
            dry_run=self._dry_run,
        )

    @staticmethod
    def _venue_order_id(order_id: str):
        from nautilus_trader.model.identifiers import VenueOrderId

        return VenueOrderId(order_id)

    async def _on_order_update(self, update: OrderUpdate) -> None:
        context = self._order_context.get(update.order_id)
        if context is None:
            self._log.warning(f"Order update for unknown order_id={update.order_id}, ignoring")
            return

        record = find_record_by_real_instrument_id(self._mapper, context["instrument_id"])

        # See module docstring: only the confirmed-equivalent statuses are
        # handled explicitly. Anything else is logged, not guessed at.
        if update.order_status in ("SUCCESS", "EXECUTED"):
            self.generate_order_filled(
                strategy_id=context["strategy_id"],
                instrument_id=context["instrument_id"],
                client_order_id=context["client_order_id"],
                venue_order_id=self._venue_order_id(update.order_id),
                venue_position_id=None,
                trade_id=self._trade_id(update.order_id),
                order_side=context["side"],
                order_type=context["order_type"],
                last_qty=Quantity.from_int(update.filled_quantity),
                last_px=Price(update.average_price, record.tick_size and 2 or 2),
                quote_currency=INR,
                commission=Money(0, INR),  # actual commission comes from trade-book, not this feed
                liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
                ts_event=update.timestamp_ns,
            )
        elif update.order_status in ("CANCELLED",):
            self.generate_order_canceled(
                strategy_id=context["strategy_id"],
                instrument_id=context["instrument_id"],
                client_order_id=context["client_order_id"],
                venue_order_id=self._venue_order_id(update.order_id),
                ts_event=update.timestamp_ns,
            )
        elif update.order_status in ("REJECTED", "FAILED", "ABORTED"):
            self.generate_order_rejected(
                strategy_id=context["strategy_id"],
                instrument_id=context["instrument_id"],
                client_order_id=context["client_order_id"],
                reason=f"INDstocks order_status={update.order_status}",
                ts_event=update.timestamp_ns,
            )
        else:
            self._log.info(
                f"Unhandled/unconfirmed order_status '{update.order_status}' for "
                f"order_id={update.order_id} — see module docstring, extend the "
                f"mapping once verified against live WS traffic."
            )

    @staticmethod
    def _trade_id(order_id: str):
        from nautilus_trader.model.identifiers import TradeId

        return TradeId(f"{order_id}-fill")
