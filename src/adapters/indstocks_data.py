"""
adapters/indstocks_data.py

WebSocket client for INDstocks' live price feed. Connects to
wss://ws-prices.indstocks.com/api/v1/ws/prices, subscribes to a watchlist
of instruments, and pushes normalized tick events to a callback.

Schema confirmed against https://api-docs.indstocks.com/Websockets/:

Subscribe message:
    {"action": "subscribe", "mode": "ltp"|"quote", "instruments": ["NSE:2885"]}

LTP mode response (confirmed, exact shape from docs):
    {"mode": "ltp", "instrument": "2885", "timestamp": 1750138351089,
     "data": {"ltp": 1426}}

Note the response's "instrument" field is the bare SECURITY_ID ("2885"),
NOT the "EXCH:SECURITY_ID" token you subscribed with ("NSE:2885") — the
exchange prefix is dropped in the response. This module re-attaches it
using the same token list you subscribed with, since a single feed
connection may span multiple exchanges and the bare ID alone doesn't
disambiguate NSE:2885 from a hypothetical BSE:2885.

Quote mode response (confirmed by logs/raw_quote_capture.jsonl):
    {"mode": "quote", "instrument": "61593", "timestamp": 1787546261198,
     "data": {"open": 320, "high": 346.6, "low": 308,
              "close": 311.3, "volume": 2271685}}

Quote mode does not provide bid/ask prices, quantities, or order-book depth.

Run standalone to sanity check your feed:
    python -m src.adapters.indstocks_data
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable

import websockets

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawTick:
    """Normalized tick, independent of Nautilus, ready for either live
    strategy consumption or recording to the Parquet catalog."""

    ws_token: str  # full "EXCH:SECURITY_ID" token, e.g. "NSE:2885"
    ltp: float
    timestamp_ns: int
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: int | None = None

    # kept for backward compat with strategy code written against the
    # earlier draft's field name
    @property
    def indstocks_token(self) -> str:
        return self.ws_token


@dataclass(frozen=True)
class QuoteMessage:
    """Validated quote-mode envelope captured from the INDstocks feed."""

    instrument: str
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: int

    @classmethod
    def from_payload(cls, payload: dict) -> "QuoteMessage":
        if payload.get("mode") != "quote":
            raise ValueError("quote payload must have mode='quote'")
        instrument = payload.get("instrument")
        timestamp = payload.get("timestamp")
        data = payload.get("data")
        if not isinstance(instrument, str) or not instrument:
            raise ValueError("quote payload requires a non-empty instrument")
        if not isinstance(timestamp, int):
            raise ValueError("quote payload requires an integer timestamp")
        if not isinstance(data, dict):
            raise ValueError("quote payload requires a data object")

        required = ("open", "high", "low", "close", "volume")
        missing = [field for field in required if field not in data]
        if missing:
            raise ValueError(f"quote payload missing required fields: {missing}")
        if any(not isinstance(data[field], (int, float)) for field in required[:-1]):
            raise ValueError("quote OHLC fields must be numeric")
        if not isinstance(data["volume"], int):
            raise ValueError("quote volume must be an integer")

        return cls(
            instrument=instrument,
            timestamp_ms=timestamp,
            open=float(data["open"]),
            high=float(data["high"]),
            low=float(data["low"]),
            close=float(data["close"]),
            volume=data["volume"],
        )


TickHandler = Callable[[RawTick], Awaitable[None]]


class IndstocksPriceFeed:
    def __init__(
        self,
        access_token: str,
        ws_url: str = "wss://ws-prices.indstocks.com/api/v1/ws/prices",
        mode: str = "quote",  # "ltp" | "quote" — quote needed for bid/ask/OBI features
        reconnect_backoff: list[float] | None = None,
    ):
        self._access_token = access_token
        self._ws_url = ws_url
        self._mode = mode
        self._backoff = reconnect_backoff or [1, 2, 5, 10, 30]
        self._subscribed_tokens: set[str] = set()
        # bare SECURITY_ID -> full "EXCH:SECURITY_ID" token, so we can
        # re-attach the exchange the response payload drops.
        self._security_id_to_token: dict[str, str] = {}
        self._handlers: list[TickHandler] = []
        self._running = False

    def add_handler(self, handler: TickHandler) -> None:
        self._handlers.append(handler)

    def subscribe(self, tokens: list[str]) -> None:
        """tokens are full 'EXCH:SECURITY_ID' strings, e.g. 'NSE:2885'."""
        self._subscribed_tokens.update(tokens)
        for token in tokens:
            _, _, security_id = token.partition(":")
            self._security_id_to_token[security_id] = token

    def unsubscribe(self, tokens: list[str]) -> None:
        """Remove tokens from future and current feed subscriptions."""
        for token in tokens:
            self._subscribed_tokens.discard(token)
            _, _, security_id = token.partition(":")
            if self._security_id_to_token.get(security_id) == token:
                del self._security_id_to_token[security_id]

    async def run_forever(self) -> None:
        self._running = True
        attempt = 0

        while self._running:
            try:
                async with websockets.connect(
                    self._ws_url,
                    additional_headers={"Authorization": self._access_token},
                ) as ws:
                    attempt = 0
                    await self._subscribe_all(ws)
                    await self._consume(ws)
            except (websockets.exceptions.ConnectionClosed, OSError) as exc:
                delay = self._backoff[min(attempt, len(self._backoff) - 1)]
                logger.warning(
                    "Price feed disconnected (%s). Reconnecting in %ss...",
                    exc,
                    delay,
                )
                attempt += 1
                await asyncio.sleep(delay)

    def stop(self) -> None:
        self._running = False

    async def _subscribe_all(self, ws) -> None:
        if not self._subscribed_tokens:
            logger.warning("No tokens subscribed — call .subscribe() first")
            return
        message = {
            "action": "subscribe",
            "mode": self._mode,
            "instruments": list(self._subscribed_tokens),
        }
        await ws.send(json.dumps(message))
        logger.info(
            "Subscribed to %d instruments in '%s' mode",
            len(self._subscribed_tokens),
            self._mode,
        )

    async def _consume(self, ws) -> None:
        async for raw_message in ws:
            try:
                payload = json.loads(raw_message)
            except json.JSONDecodeError:
                logger.debug("Non-JSON message (likely heartbeat): %r", raw_message)
                continue

            # Ensure payload is a dict; skip if it's a JSON string or other type
            if not isinstance(payload, dict):
                logger.debug("Non-dict message (likely heartbeat): %r", raw_message)
                continue

            tick = self._parse_tick(payload)
            if tick is None:
                continue

            for handler in self._handlers:
                await handler(tick)

    def _parse_tick(self, payload: dict) -> RawTick | None:
        """
        Confirmed LTP-mode shape:
            {"mode": "ltp", "instrument": "2885", "timestamp": <ms epoch>,
             "data": {"ltp": 1426}}

        Quote-mode data is validated by QuoteMessage. The captured schema
        has OHLCV only, so close is used as the normalized LTP and no
        bid/ask/depth values are invented here.
        """
        mode = payload.get("mode")
        if mode not in ("ltp", "quote"):
            return None  # heartbeat or an envelope shape we don't handle yet

        if mode == "quote":
            message = QuoteMessage.from_payload(payload)
            security_id = message.instrument
            ts = message.timestamp_ms
            ltp = message.close
            volume = message.volume
        else:
            security_id = payload.get("instrument")
            ts = payload.get("timestamp")
            data = payload.get("data") or {}
            ltp = data.get("ltp")
            volume = data.get("volume")

        if security_id is None or ltp is None or ts is None:
            return None

        token = self._security_id_to_token.get(str(security_id))
        if token is None:
            logger.warning(
                "Tick for unrecognized security_id '%s' — not in our "
                "subscribed set, dropping", security_id,
            )
            return None

        return RawTick(
            ws_token=token,
            ltp=float(ltp),
            timestamp_ns=int(ts) * 1_000_000,  # ms epoch -> ns, confirmed by docs example
            volume=volume,
        )


# -----------------------------------------------------------------------
# Nautilus LiveMarketDataClient wiring: see src/adapters/nautilus_live_data_client.py
# (IndstocksLiveMarketDataClient), which wraps this class for use inside a real
# nautilus_trader TradingNode.
# -----------------------------------------------------------------------


async def _demo_handler(tick: RawTick) -> None:
    print(tick)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("INDSTOCKS_ACCESS_TOKEN")
    if not token:
        raise SystemExit("Set INDSTOCKS_ACCESS_TOKEN env var to run this demo")

    feed = IndstocksPriceFeed(access_token=token, mode="quote")
    feed.add_handler(_demo_handler)
    feed.subscribe(["NSE:2885"])  # RELIANCE, per the docs' own example — swap for your instrument
    print("Connecting... watch the printed ticks and compare 'data' keys against")
    print("the TODOs in _parse_tick() before trusting quote-mode fields.")
    await feed.run_forever()


if __name__ == "__main__":
    asyncio.run(_main())
