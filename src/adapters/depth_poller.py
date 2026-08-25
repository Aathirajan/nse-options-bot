"""
adapters/depth_poller.py

Periodic REST market-depth poller with adaptive rate limiting for INDstocks.

Real WS quote-mode payloads have NO bid/ask/depth data — only OHLCV.
This poller fetches top-of-book depth via GET /market/quotes/mkt
and maintains a cache of latest depth per instrument.

Adaptive rate limiting:
- Starts at configurable interval (default 500ms)
- On 429 or Retry-After header: immediate exponential backoff
- On sustained success: gradually ease back down toward floor (default 200ms)
- Never goes below floor regardless of success streak
- Logs interval changes at INFO so discovered rate limit is visible
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from src.adapters.indstocks_market_data import IndstocksMarketDataClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LatestDepth:
    """Top-of-book depth snapshot from REST poll."""
    ws_token: str           # Full WS token e.g. "NSE:2885" or "NFO:51011"
    scrip_code: str         # REST scrip code e.g. "NSE_2885"
    bid_price: float
    bid_qty: int
    ask_price: float
    ask_qty: int
    timestamp_ns: int       # When this depth was fetched

    @property
    def spread_pct(self) -> float:
        """Bid-ask spread as percentage of bid price."""
        if self.bid_price <= 0:
            return 0.0
        return ((self.ask_price - self.bid_price) / self.bid_price) * 100

    def is_stale(self, max_age_seconds: float = 10.0) -> bool:
        """Check if depth is older than max_age_seconds."""
        age_ns = time.time_ns() - self.timestamp_ns
        return age_ns > max_age_seconds * 1_000_000_000


class DepthPoller:
    """
    Polls market depth for multiple instruments via REST API with adaptive rate limiting.
    
    Usage:
        poller = DepthPoller(market_data_client, settings)
        poller.add_instrument(ws_token, scrip_code)
        await poller.start()
        depth = poller.get_latest_depth(ws_token)
        await poller.stop()
    """

    def __init__(
        self,
        market_data: IndstocksMarketDataClient,
        settings: dict,
        mapper: "InstrumentMapper",
    ):
        """
        Args:
            market_data: IndstocksMarketDataClient instance for REST calls
            settings: Full settings dict (reads market_depth.* keys)
            mapper: InstrumentMapper for ws_token <-> scrip_code conversion
        """
        self._market_data = market_data
        self._mapper = mapper
        self._running = False
        self._task: asyncio.Task | None = None
        
        # Configuration from settings
        md_settings = settings.get("market_depth", {})
        self._interval_seconds = md_settings.get("initial_interval_seconds", 0.5)
        self._floor_seconds = md_settings.get("floor_seconds", 0.2)
        self._max_backoff_seconds = md_settings.get("max_backoff_seconds", 10.0)
        self._staleness_seconds = md_settings.get("staleness_seconds", 10.0)
        
        # Instrument tracking: ws_token -> scrip_code
        self._instruments: dict[str, str] = {}  # ws_token -> scrip_code
        
        # Cache: ws_token -> LatestDepth
        self._cache: dict[str, LatestDepth] = {}
        
        # Adaptive rate limiting state
        self._success_streak = 0
        self._consecutive_429 = 0
        self._last_interval_change_log = 0.0

    @property
    def current_interval_seconds(self) -> float:
        """Current polling interval (for monitoring/logging)."""
        return self._interval_seconds

    def add_instrument(self, ws_token: str, scrip_code: str | None = None) -> None:
        """Add an instrument to the polling set.
        
        If scrip_code not provided, derives it from ws_token via mapper.
        """
        if scrip_code is None:
            # ws_token format: "EXCH:SECURITY_ID" -> scrip_code: "EXCH_SECURITY_ID"
            parts = ws_token.split(":", 1)
            if len(parts) == 2:
                scrip_code = f"{parts[0]}_{parts[1]}"
            else:
                logger.warning("Cannot derive scrip_code from ws_token '%s'", ws_token)
                return
        self._instruments[ws_token] = scrip_code
        logger.info("DepthPoller: added instrument %s (scrip_code=%s)", ws_token, scrip_code)

    def remove_instrument(self, ws_token: str) -> None:
        """Remove an instrument from the polling set."""
        if ws_token in self._instruments:
            del self._instruments[ws_token]
            # Keep cache for potential reuse, but it will go stale
            logger.info("DepthPoller: removed instrument %s", ws_token)

    def get_latest_depth(self, ws_token: str) -> LatestDepth | None:
        """Get latest cached depth for an instrument.
        
        Returns None if no depth has been polled yet or if stale.
        """
        depth = self._cache.get(ws_token)
        if depth is None:
            return None
        if depth.is_stale(self._staleness_seconds):
            logger.debug("DepthPoller: cached depth for %s is stale (age > %.1fs)", ws_token, self._staleness_seconds)
            return None
        return depth

    def _parse_depth_response(self, ws_token: str, data: dict) -> LatestDepth | None:
        """Parse INDstocks market depth response into LatestDepth.
        
        Expected response format per docstring:
        {
            "NSE_2885": {
                "buy": [{"price": "1426.0", "quantity": "1,234"}, ...],
                "sell": [{"price": "1427.0", "quantity": "567"}, ...]
            }
        }
        Quantities may contain thousands separators (e.g. "2,318").
        """
        try:
            scrip_code = self._instruments.get(ws_token)
            if not scrip_code:
                return None
            
            entry = data.get(scrip_code)
            if not entry:
                logger.debug("DepthPoller: no depth data for scrip_code %s", scrip_code)
                return None
            
            # Get top of book (first level)
            buy_levels = entry.get("buy", [])
            sell_levels = entry.get("sell", [])
            
            if not buy_levels or not sell_levels:
                logger.debug("DepthPoller: empty buy/sell levels for %s", scrip_code)
                return None
            
            # Parse top bid
            top_bid = buy_levels[0]
            bid_price = float(top_bid["price"])
            bid_qty = int(top_bid["quantity"].replace(",", ""))
            
            # Parse top ask
            top_ask = sell_levels[0]
            ask_price = float(top_ask["price"])
            ask_qty = int(top_ask["quantity"].replace(",", ""))
            
            if bid_price <= 0 or ask_price <= 0:
                logger.debug("DepthPoller: invalid prices for %s: bid=%s ask=%s", scrip_code, bid_price, ask_price)
                return None
            
            return LatestDepth(
                ws_token=ws_token,
                scrip_code=scrip_code,
                bid_price=bid_price,
                bid_qty=bid_qty,
                ask_price=ask_price,
                ask_qty=ask_qty,
                timestamp_ns=time.time_ns(),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("DepthPoller: failed to parse depth for %s: %s", ws_token, exc)
            return None

    async def _poll_once(self) -> None:
        """Single polling cycle for all subscribed instruments."""
        if not self._instruments:
            return
        
        scrip_codes = list(self._instruments.values())
        logger.debug("DepthPoller: polling %d instruments: %s", len(scrip_codes), scrip_codes)
        
        try:
            response = await self._market_data.get_market_depth(scrip_codes)
            
            # Check for rate limit headers in response (httpx stores them in response.headers)
            # Note: we need to capture the response object to check headers, but get_market_depth
            # only returns the JSON data. We'll handle 429 via exception instead.
            
            updated = 0
            for ws_token in self._instruments:
                depth = self._parse_depth_response(ws_token, response)
                if depth:
                    self._cache[ws_token] = depth
                    updated += 1
            
            if updated:
                logger.debug("DepthPoller: updated depth for %d/%d instruments", updated, len(self._instruments))
            
            # Successful poll - adaptive rate easing
            self._success_streak += 1
            self._consecutive_429 = 0
            self._maybe_ease_interval()
            
        except Exception as exc:
            # Check if it's a 429 (rate limited)
            if hasattr(exc, 'response') and exc.response is not None:
                status = exc.response.status_code
                if status == 429:
                    self._handle_429(exc)
                    return
            # For other errors (timeout, 5xx, etc.), log and keep cache
            logger.warning("DepthPoller: poll failed: %s", exc)
            # Don't clear cache on failure - keep last known good depth

    def _handle_429(self, exc: Exception) -> None:
        """Handle HTTP 429 rate limit response with exponential backoff."""
        retry_after = None
        if hasattr(exc, 'response') and exc.response is not None:
            retry_after_header = exc.response.headers.get("Retry-After")
            if retry_after_header:
                try:
                    retry_after = float(retry_after_header)
                except ValueError:
                    pass
        
        if retry_after is not None:
            new_interval = max(retry_after, self._interval_seconds * 2)
        else:
            new_interval = self._interval_seconds * 2
        
        new_interval = min(new_interval, self._max_backoff_seconds)
        old_interval = self._interval_seconds
        self._interval_seconds = new_interval
        self._consecutive_429 += 1
        self._success_streak = 0
        
        logger.warning(
            "DepthPoller: HTTP 429 received, backing off from %.3fs to %.3fs (retry_after=%s)",
            old_interval, new_interval, retry_after
        )

    def _maybe_ease_interval(self) -> None:
        """Gradually ease interval back down toward floor on sustained success."""
        # Only ease after a few consecutive successes post-backoff
        if self._success_streak >= 3 and self._interval_seconds > self._floor_seconds:
            new_interval = max(self._interval_seconds / 2, self._floor_seconds)
            if new_interval < self._interval_seconds:
                old_interval = self._interval_seconds
                self._interval_seconds = new_interval
                if time.time() - self._last_interval_change_log > 30:  # Throttle log spam
                    logger.info(
                        "DepthPoller: easing interval from %.3fs to %.3fs (success streak=%d)",
                        old_interval, new_interval, self._success_streak
                    )
                    self._last_interval_change_log = time.time()

    async def _run_loop(self) -> None:
        """Main polling loop."""
        self._running = True
        while self._running:
            start = time.monotonic()
            await self._poll_once()
            elapsed = time.monotonic() - start
            sleep_time = max(0, self._interval_seconds - elapsed)
            if sleep_time > 0:
                try:
                    await asyncio.sleep(sleep_time)
                except asyncio.CancelledError:
                    break

    async def start(self) -> None:
        """Start the polling background task."""
        if self._running:
            logger.warning("DepthPoller already running")
            return
        logger.info(
            "DepthPoller starting: interval=%.3fs, floor=%.3fs, max_backoff=%.1fs, staleness=%.1fs",
            self._interval_seconds, self._floor_seconds, self._max_backoff_seconds, self._staleness_seconds
        )
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the polling background task."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("DepthPoller stopped")