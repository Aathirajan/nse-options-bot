"""
adapters/nautilus_live_data_client.py

Wraps IndstocksPriceFeed (indstocks_data.py) inside a real
nautilus_trader LiveMarketDataClient, so the live engine can subscribe
to quote ticks the same way it would for any other venue.

Overridden methods and their exact async signatures below are confirmed
by direct introspection of nautilus_trader==1.231.0's
LiveMarketDataClient — not guessed:

    async def _connect(self) -> None
    async def _disconnect(self) -> None
    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None
    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None

Data flows back into the engine via self._handle_data(quote_tick) — a
concrete method on LiveMarketDataClient, called synchronously from our
tick handler.

Known gaps (see README): this client only implements quote-tick
subscription, since that's all the current strategy set consumes. If
you later need order book depth or trade ticks, INDstocks' WS payload
schema for those isn't confirmed against live docs either — extend
IndstocksPriceFeed/_parse_tick first (see indstocks_data.py's own
TODOs), then add the matching _subscribe_order_book_deltas /
_subscribe_trade_ticks overrides here.
"""

from __future__ import annotations

import asyncio

from nautilus_trader.data.messages import SubscribeQuoteTicks, UnsubscribeQuoteTicks
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from src.adapters.indstocks_data import IndstocksPriceFeed, RawTick
from src.adapters.instrument_builder import build_instrument, find_record_by_real_instrument_id
from src.adapters.mapper import InstrumentMapper


class IndstocksLiveMarketDataClient(LiveMarketDataClient):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id,
        venue,
        msgbus,
        cache,
        clock,
        instrument_provider,
        access_token: str,
        mapper: InstrumentMapper,
        ws_url: str = "wss://ws-prices.indstocks.com/api/v1/ws/prices",
        initial_ws_tokens: list[str] | None = None,
        config=None,
    ):
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=venue,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        self._mapper = mapper
        self._feed = IndstocksPriceFeed(access_token=access_token, ws_url=ws_url, mode="quote")
        if initial_ws_tokens:
            self._feed.subscribe(initial_ws_tokens)
        self._feed.add_handler(self._on_raw_tick)
        self._feed_task: asyncio.Task | None = None
        # Nautilus InstrumentId string -> price precision, needed to build
        # Price()/Quantity() objects correctly per-instrument.
        self._price_precision_by_instrument: dict[str, int] = {}
        # our nautilus_id convention (e.g. "NIFTY-INDEX") -> the REAL
        # nautilus_trader InstrumentId (e.g. "NIFTY.NSE") built by
        # instrument_builder — these are NOT the same string, see
        # instrument_builder.py's docstring for why.
        self._real_instrument_id: dict[str, InstrumentId] = {}

    async def _connect(self) -> None:
        self._feed_task = self._loop.create_task(self._feed.run_forever())
        # give the connection a moment before declaring ourselves connected;
        # IndstocksPriceFeed handles its own reconnect/backoff after this.
        await asyncio.sleep(0)

    async def _disconnect(self) -> None:
        self._feed.stop()
        if self._feed_task is not None:
            self._feed_task.cancel()
            try:
                await self._feed_task
            except asyncio.CancelledError:
                pass

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        # command.instrument_id is the REAL Nautilus id (e.g. "NIFTY.NSE"),
        # not our mapper's internal "NIFTY-INDEX" convention — look up the
        # instrument by matching build_instrument()'s id against our records.
        record = find_record_by_real_instrument_id(self._mapper, command.instrument_id)
        if record is None:
            self._log.error(
                f"No instrument record produces real id '{command.instrument_id}' "
                f"— is instruments.json stale?"
            )
            return

        self._real_instrument_id[record.nautilus_id] = command.instrument_id
        self._price_precision_by_instrument[record.nautilus_id] = self._precision_from_tick_size(
            record.tick_size
        )
        self._feed.subscribe([record.ws_token])
        self._log.info(f"Subscribed quote ticks: {command.instrument_id} -> {record.ws_token}")

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        record = find_record_by_real_instrument_id(self._mapper, command.instrument_id)
        if record is None:
            return
        self._feed.unsubscribe([record.ws_token])
        self._real_instrument_id.pop(record.nautilus_id, None)
        self._price_precision_by_instrument.pop(record.nautilus_id, None)

    @staticmethod
    def _precision_from_tick_size(tick_size: float) -> int:
        s = f"{tick_size:.10f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0

    @staticmethod
    def _precision_from_price(price: float) -> int:
        s = f"{price:.10f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0

    async def _on_raw_tick(self, tick: RawTick) -> None:
        record = self._mapper.from_ws_token_string(tick.ws_token)
        if record is None:
            return  # tick for a token we don't have mapped, drop it

        real_id = self._real_instrument_id.get(record.nautilus_id)
        if real_id is None:
            return  # not currently subscribed via Nautilus, drop it

        precision = self._price_precision_by_instrument.get(
            record.nautilus_id, self._precision_from_tick_size(record.tick_size)
        )
        precision = max(precision, self._precision_from_price(tick.ltp))

        if tick.bid is None or tick.ask is None:
            # LTP-mode or a quote tick missing bid/ask — synthesize a
            # zero-spread quote so the engine still gets a data point.
            # Prefer running in "quote" mode (the default here) over
            # "ltp" mode for any strategy that depends on real spread.
            bid = ask = tick.ltp
        else:
            bid, ask = tick.bid, tick.ask

        quote_tick = QuoteTick(
            instrument_id=real_id,
            bid_price=Price(bid, precision),
            ask_price=Price(ask, precision),
            bid_size=Quantity.from_int(int(tick.bid_qty or 1)),
            ask_size=Quantity.from_int(int(tick.ask_qty or 1)),
            ts_event=tick.timestamp_ns,
            ts_init=tick.timestamp_ns,
        )
        self._handle_data(quote_tick)
