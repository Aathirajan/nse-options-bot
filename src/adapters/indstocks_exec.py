"""
adapters/indstocks_exec.py

REST client for order placement/modification/cancellation against
INDstocks, plus a WebSocket listener for order status updates.

Schemas below are confirmed against
https://api-docs.indstocks.com/normal_orders/ and
https://api-docs.indstocks.com/Websockets/#order-updates-feed — not
guesses.

Place order — POST /order:
    {"txn_type": "BUY"|"SELL", "exchange": "NSE"|"BSE",
     "segment": "DERIVATIVE"|"EQUITY", "product": "MARGIN"|"INTRADAY"|"CNC",
     "order_type": "LIMIT"|"MARKET", "validity": "DAY"|"IOC",
     "security_id": "<bare numeric id>", "qty": int,
     "algo_id": "99999" (NSE) | "9999999999999999" (BSE),
     "limit_price": float (required if LIMIT),
     "is_amo": bool (optional), "remarks": str (optional, <=100 chars)}
  -> {"status": "success", "data": {"order_id": "...", "order_status": "INITIATED"}}

IMPORTANT: MARKET orders are NOT true market orders — INDstocks converts
them to a LIMIT order at the current live price before routing to the
exchange. There's no way to guarantee fill-at-any-price; plan slippage
buffers accordingly (see run_live.py's limit_price handling).

Order status vocabulary (REST order book / order details) — confirmed
via the full OpenAPI spec enum, which does NOT include the WebSocket
order-update feed's own worked example value ("PARTIALLY_EXECUTED",
underscore) at all — the closest REST equivalent is "PARTIALLY FILLED"
(space, no underscore). This is a genuine inconsistency in INDstocks'
own docs (their WS guide's example literally doesn't appear in their
OpenAPI schema's enum), not something resolvable by more careful
reading — see OrderUpdate's docstring below for how this code handles it:
  QUEUED, O-PENDING, SL-PENDING, PROCESSING, ABORTED, INITIATED, SUCCESS,
  CANCELLED, MODIFIED, PENDING, EXPIRED, FAILED, "PARTIALLY FILLED",
  "PARTIALLY FILLED - CANCELLED", "PARTIALLY FILLED - EXPIRED"

FIELD NAME WARNING: place/modify/cancel responses use "order_id" and
"order_status". The READ endpoints (GET /order-book, GET /order) use
different keys for the same concepts: "id" and "status". Confirmed from
the docs' own worked examples — don't assume one key-name set across
both groups of endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
import websockets

logger = logging.getLogger(__name__)

# Confirmed algo_id constants for API-originated orders (docs: "Use '99999'
# for NSE, '9999999999999999' for BSE orders").
ALGO_ID_NSE = "99999"
ALGO_ID_BSE = "9999999999999999"

# Reserved remarks value INDstocks rejects (matched case/whitespace-
# insensitively against the stored, truncated value).
RESERVED_REMARK = "TV-TERMINAL"


@dataclass(frozen=True)
class OrderRequest:
    security_id: str          # bare numeric id, e.g. "2885" — NOT "NSE:2885"
    exchange: str              # "NSE" | "BSE"
    segment: str                # "DERIVATIVE" | "EQUITY"
    side: str                    # "BUY" | "SELL"
    quantity: int
    order_type: str = "LIMIT"     # "LIMIT" | "MARKET" (MARKET -> LIMIT-at-live-price)
    limit_price: float | None = None
    product_type: str = "INTRADAY"  # "MARGIN" | "INTRADAY" | "CNC"
    validity: str = "DAY"            # "DAY" | "IOC"
    is_amo: bool = False
    remarks: str | None = None       # free-text tag, <=100 chars, echoed back on reads

    def algo_id(self) -> str:
        return ALGO_ID_NSE if self.exchange == "NSE" else ALGO_ID_BSE


@dataclass(frozen=True)
class PlaceOrderResult:
    order_id: str
    order_status: str


@dataclass(frozen=True)
class OrderUpdate:
    """
    From the Order Updates WebSocket. Note: the WS payload's
    order_status vocabulary ("PARTIALLY_EXECUTED" with an underscore, per
    the WS docs' own example) does not obviously match the REST order
    book's vocabulary ("PARTIALLY FILLED" with a space, no underscore) —
    treat status as an opaque string rather than a strict enum until you
    confirm the full WS status vocabulary against live traffic. This
    class deliberately keeps order_status as `str`, not an Enum, for
    that reason.
    """

    order_id: str
    order_status: str
    filled_quantity: int
    remaining_quantity: int
    average_price: float
    timestamp_ns: int


OrderUpdateHandler = Callable[[OrderUpdate], Awaitable[None]]


class IndstocksExecutionClient:
    def __init__(
        self,
        access_token: str,
        rest_base_url: str = "https://api.indstocks.com",
        ws_orders_url: str = "wss://ws-order-updates.indstocks.com/api/v1/ws/trades",
    ):
        self._access_token = access_token
        self._rest_base_url = rest_base_url
        self._ws_orders_url = ws_orders_url
        self._http = httpx.AsyncClient(
            base_url=rest_base_url,
            headers={"Authorization": access_token, "Content-Type": "application/json"},
            timeout=10.0,
        )
        self._update_handlers: list[OrderUpdateHandler] = []
        self._running = False

    def add_order_update_handler(self, handler: OrderUpdateHandler) -> None:
        self._update_handlers.append(handler)

    @staticmethod
    def _validate_remarks(remarks: str | None) -> str | None:
        if remarks is None:
            return None
        truncated = remarks[:100]
        if truncated.strip().upper() == RESERVED_REMARK:
            raise ValueError(
                f"remarks resolves to the reserved value '{RESERVED_REMARK}' "
                f"after truncation/case-normalization — choose a different tag."
            )
        return truncated

    async def place_order(self, request: OrderRequest, *, dry_run: bool = True) -> PlaceOrderResult:
        """
        Places an order via POST /order.

        dry_run=True (default) logs the intended payload and returns a
        fake order id instead of hitting the live API. Flip this only
        once you've validated PAPER-mode decisions over a full session.
        """
        remarks = self._validate_remarks(request.remarks)

        payload = {
            "txn_type": request.side,
            "exchange": request.exchange,
            "segment": request.segment,
            "product": request.product_type,
            "order_type": request.order_type,
            "validity": request.validity,
            "security_id": request.security_id,
            "qty": request.quantity,
            "algo_id": request.algo_id(),
            "is_amo": request.is_amo,
        }
        if request.order_type == "LIMIT":
            if request.limit_price is None:
                raise ValueError("limit_price is required when order_type is LIMIT")
            payload["limit_price"] = request.limit_price
        if remarks:
            payload["remarks"] = remarks

        if dry_run:
            logger.info("[DRY RUN] would POST /order: %s", payload)
            return PlaceOrderResult(order_id=f"DRYRUN-{request.security_id}-{request.side}", order_status="DRY_RUN")

        response = await self._http.post("/order", json=payload)
        response.raise_for_status()
        body = response.json()
        data = body["data"]
        logger.info("Order placed: %s -> %s (%s)", payload, data["order_id"], data["order_status"])
        return PlaceOrderResult(order_id=data["order_id"], order_status=data["order_status"])

    async def modify_order(
        self,
        order_id: str,
        segment: str,
        *,
        qty: int,
        limit_price: float,
        dry_run: bool = True,
    ) -> PlaceOrderResult:
        """POST /order/modify. Note: does NOT accept remarks — the order
        keeps whatever remark it was placed with."""
        payload = {"order_id": order_id, "segment": segment, "qty": qty, "limit_price": limit_price}

        if dry_run:
            logger.info("[DRY RUN] would POST /order/modify: %s", payload)
            return PlaceOrderResult(order_id=order_id, order_status="DRY_RUN")

        response = await self._http.post("/order/modify", json=payload)
        response.raise_for_status()
        data = response.json()["data"]
        return PlaceOrderResult(order_id=data["order_id"], order_status=data["order_status"])

    async def cancel_order(self, order_id: str, segment: str, *, dry_run: bool = True) -> None:
        """POST /order/cancel."""
        payload = {"order_id": order_id, "segment": segment}

        if dry_run:
            logger.info("[DRY RUN] would POST /order/cancel: %s", payload)
            return

        response = await self._http.post("/order/cancel", json=payload)
        response.raise_for_status()

    async def get_order_book(self) -> list[dict]:
        """
        GET /order-book — all orders placed today.

        FIELD NAME WARNING: unlike /order, /order/modify, /order/cancel
        (which return "order_id"/"order_status"), each entry here uses
        "id" and "status" instead — confirmed from the real docs'
        worked example. Don't assume the same key names across
        place/modify/cancel vs. these read endpoints.
        """
        response = await self._http.get("/order-book")
        response.raise_for_status()
        return response.json()["data"]

    async def get_order_details(self, order_id: str, segment: str) -> dict:
        """
        GET /order — takes order_id/segment as a JSON body, per docs.

        Same field-name note as get_order_book(): the returned dict uses
        "id"/"status", not "order_id"/"order_status".
        """
        response = await self._http.request("GET", "/order", json={"order_id": order_id, "segment": segment})
        response.raise_for_status()
        return response.json()["data"]

    async def get_order_trades(self, order_id: str, segment: str) -> list[dict]:
        """
        GET /order/trades — fills for ONE order (fill_id, exch_order_id,
        quantity, price, trade_date). Does NOT include `remarks` — use
        get_trade_book() instead if you need the tag alongside each fill.
        """
        response = await self._http.request(
            "GET", "/order/trades", json={"order_id": order_id, "segment": segment}
        )
        response.raise_for_status()
        return response.json()["data"]

    async def get_trade_book(self, segment: str) -> list[dict]:
        """GET /trade-book?segment=... — includes `remarks` per fill,
        useful for reconciling fills against your own strategy_id."""
        response = await self._http.get("/trade-book", params={"segment": segment})
        response.raise_for_status()
        return response.json()["data"]

    async def get_positions(self, segment: str, product: str) -> list[dict]:
        """GET /portfolio/positions for the broker's current positions."""
        response = await self._http.get(
            "/portfolio/positions",
            params={"segment": segment.lower(), "product": product.lower()},
        )
        response.raise_for_status()
        return response.json()["data"]

    async def close(self) -> None:
        await self._http.aclose()

    # -- Order update stream -------------------------------------------

    async def run_order_updates_forever(self) -> None:
        self._running = True
        backoff = [1, 2, 5, 10, 30]
        attempt = 0

        while self._running:
            try:
                async with websockets.connect(
                    self._ws_orders_url,
                    additional_headers={"Authorization": self._access_token},
                ) as ws:
                    attempt = 0
                    await ws.send(json.dumps({"action": "subscribe", "mode": "order_update"}))
                    async for raw_message in ws:
                        update = self._parse_update(raw_message)
                        if update is None:
                            continue
                        for handler in self._update_handlers:
                            await handler(update)
            except (websockets.exceptions.ConnectionClosed, OSError) as exc:
                delay = backoff[min(attempt, len(backoff) - 1)]
                logger.warning("Order updates disconnected (%s). Retrying in %ss", exc, delay)
                attempt += 1
                await asyncio.sleep(delay)

    def stop(self) -> None:
        self._running = False

    @staticmethod
    def _parse_update(raw_message: str) -> OrderUpdate | None:
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return None  # likely a heartbeat

        if payload.get("type") != "order":
            return None

        try:
            return OrderUpdate(
                order_id=payload["order_id"],
                order_status=payload["order_status"],
                filled_quantity=payload.get("filled_quantity", 0),
                remaining_quantity=payload.get("remaining_quantity", 0),
                average_price=float(payload.get("average_price", 0.0)),
                timestamp_ns=int(payload.get("timestamp", 0)) * 1_000_000,
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Malformed order update %r: %s", payload, exc)
            return None


# -----------------------------------------------------------------------
# TODO: Nautilus ExecutionClient wiring
#
#   class IndstocksExecutionClient(LiveExecutionClient):
#       async def _submit_order(self, command): ...   # -> place_order()
#       async def _cancel_order(self, command): ...    # -> cancel_order()
#       # order update handler translates OrderUpdate -> Nautilus OrderFilled
#       # / OrderCanceled events via self._handle_event(...)
#       # NOTE: reconcile the WS "PARTIALLY_EXECUTED"-style vocabulary
#       # against the REST "PARTIALLY FILLED"-style vocabulary before
#       # writing this translation — see OrderUpdate's docstring.
#
# Check the ABC for your pinned nautilus_trader version before wiring.
# -----------------------------------------------------------------------
