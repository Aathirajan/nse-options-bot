"""
adapters/indstocks_market_data.py

REST market-data client covering the endpoints separate from order
management: Market Quotes, the Option Chain (Options Toolkit), and
Historical Data. Schemas below are confirmed against the full INDstocks
API docs export — not guessed.

This is what resolves the ORPC strategy's "which CE/PE tokens are ATM
today" problem: option_chain() returns the full strike ladder including
each leg's security_id, last_price, and top-of-book bid/ask in one call,
keyed by strike — no separate lookup or guessing needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OptionLeg:
    security_id: str
    trading_symbol: str
    last_price: float
    previous_close_price: float
    oi: int
    previous_oi: int
    volume: int
    top_bid_price: float
    top_bid_quantity: int
    top_ask_price: float
    top_ask_quantity: int
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


@dataclass(frozen=True)
class OptionChainResult:
    underlying_ltp: float
    expiry: str
    # strike price -> (ce, pe); either leg may be None if that side is
    # absent from the response for a given strike (not expected in
    # practice, but the schema doesn't guarantee both are always present)
    strikes: dict[float, tuple[OptionLeg | None, OptionLeg | None]]

    def atm_strike(self) -> float:
        """The strike closest to the underlying's current LTP."""
        return min(self.strikes.keys(), key=lambda k: abs(k - self.underlying_ltp))

    def cheaper_leg_at(self, strike: float) -> tuple[str, OptionLeg]:
        """
        Returns ("CE"|"PE", leg) for whichever side is cheaper at a given
        strike — directly useful for the ORPC strategy's "buy whichever
        of ATM CE/PE is cheaper" entry rule.
        """
        ce, pe = self.strikes[strike]
        if ce is None:
            return "PE", pe
        if pe is None:
            return "CE", ce
        return ("CE", ce) if ce.last_price <= pe.last_price else ("PE", pe)


class IndstocksMarketDataClient:
    def __init__(
        self, 
        access_token: str, 
        rest_base_url: str = "https://api.indstocks.com",
        timeout: float = 30.0,
    ):
        """
        Args:
            access_token: INDstocks API token
            rest_base_url: Base URL for INDstocks REST API
            timeout: HTTP request timeout in seconds (default 30s for slower networks)
        """
        self._http = httpx.AsyncClient(
            base_url=rest_base_url, 
            headers={"Authorization": access_token}, 
            timeout=timeout
        )
        logger.info("Initialized IndstocksMarketDataClient with %.1f second timeout", timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def list_expiries(self, underlying: str, segment: str = "DERIVATIVE") -> list[str]:
        """
        GET /market/instruments/expiries — upcoming expiry dates for an
        underlying, ascending order ("2026-08-25", "2026-09-01", ...).
        The nearest expiry (what ORPC and most weekly-options strategies
        want) is simply the first element.
        """
        logger.info("Fetching expiries for %s (segment=%s)", underlying, segment)
        try:
            response = await self._http.get(
                "/market/instruments/expiries", params={"underlying": underlying, "segment": segment}
            )
            response.raise_for_status()
            data = response.json()["data"]
            logger.info("Received %d expiries for %s: %s", len(data), underlying, data[:3] if len(data) > 3 else data)
            return data
        except httpx.TimeoutException as e:
            logger.error("Timeout fetching expiries for %s: %s", underlying, e)
            raise RuntimeError(f"API timeout fetching expiries for {underlying}. Check your network connection and INDstocks API status.") from e
        except httpx.HTTPError as e:
            logger.error("HTTP error fetching expiries for %s: %s", underlying, e)
            raise RuntimeError(f"API error fetching expiries for {underlying}: {e}") from e

    async def option_chain(
        self,
        *,
        exchange: str,          # "NSE" | "BSE"
        segment: str,            # "INDEX" | "EQUITY" -- the UNDERLYING's segment, not DERIVATIVE
        underlying_scrip: str,   # security_id of the underlying (NOT an option contract)
        expiry: str,              # "YYYY-MM-DD"
        strike_count: int = 10,   # strikes per side of ATM; response has (2*strike_count)+1 strikes
    ) -> OptionChainResult:
        """
        GET /market/option-chain — full strike ladder with Greeks/IV.

        Note the error envelope on this endpoint is NOT the standard
        {"status": "error", ...} shape used elsewhere — check the HTTP
        status first, then read whichever of debug_info/message/error
        is present on a non-2xx response.
        """
        logger.info(
            "Fetching option chain: exchange=%s segment=%s underlying_scrip=%s expiry=%s strike_count=%d",
            exchange, segment, underlying_scrip, expiry, strike_count
        )
        try:
            response = await self._http.get(
                "/market/option-chain",
                params={
                    "exchange": exchange,
                    "segment": segment,
                    "underlying-scrip": underlying_scrip,
                    "expiry": expiry,
                    "strike_count": strike_count,
                },
            )
            response.raise_for_status()
            body = response.json()["data"]

            strikes: dict[float, tuple[OptionLeg | None, OptionLeg | None]] = {}
            for strike_str, legs in body["strikes"].items():
                strikes[float(strike_str)] = (
                    self._parse_leg(legs.get("ce")),
                    self._parse_leg(legs.get("pe")),
                )

            result = OptionChainResult(
                underlying_ltp=body["underlying_ltp"],
                expiry=body["expiry"],
                strikes=strikes,
            )
            logger.info("Received option chain with %d strikes, underlying_ltp=%.2f", len(strikes), result.underlying_ltp)
            return result
        except httpx.TimeoutException as e:
            logger.error("Timeout fetching option chain for %s/%s: %s", underlying_scrip, expiry, e)
            raise RuntimeError(f"API timeout fetching option chain. Check your network connection and INDstocks API status.") from e
        except httpx.HTTPError as e:
            logger.error("HTTP error fetching option chain: %s", e)
            raise RuntimeError(f"API error fetching option chain: {e}") from e

    @staticmethod
    def _parse_leg(leg: dict | None) -> OptionLeg | None:
        if leg is None:
            return None
        greeks = leg.get("greeks", {})
        return OptionLeg(
            security_id=leg["security_id"],
            trading_symbol=leg["trading_symbol"],
            last_price=leg["last_price"],
            previous_close_price=leg["previous_close_price"],
            oi=leg["oi"],
            previous_oi=leg["previous_oi"],
            volume=leg["volume"],
            top_bid_price=leg["top_bid_price"],
            top_bid_quantity=leg["top_bid_quantity"],
            top_ask_price=leg["top_ask_price"],
            top_ask_quantity=leg["top_ask_quantity"],
            iv=leg["iv"],
            delta=greeks.get("delta", 0.0),
            gamma=greeks.get("gamma", 0.0),
            theta=greeks.get("theta", 0.0),
            vega=greeks.get("vega", 0.0),
        )

    async def get_quotes_ltp(self, scrip_codes: list[str]) -> dict[str, float]:
        """GET /market/quotes/ltp?scrip-codes=NSE_3045,... -> {scrip_code: live_price}."""
        response = await self._http.get(
            "/market/quotes/ltp", params={"scrip-codes": ",".join(scrip_codes)}
        )
        response.raise_for_status()
        data = response.json()["data"]
        return {code: entry["live_price"] for code, entry in data.items()}

    async def get_quotes_full(self, scrip_codes: list[str]) -> dict[str, dict]:
        """GET /market/quotes/full?scrip-codes=... -> raw per-code snapshot dicts
        (live_price, day_change, OHLC, circuit limits, market_depth, volume)."""
        response = await self._http.get(
            "/market/quotes/full", params={"scrip-codes": ",".join(scrip_codes)}
        )
        response.raise_for_status()
        return response.json()["data"]

    async def get_market_depth(self, scrip_codes: list[str]) -> dict[str, dict]:
        """
        GET /market/quotes/mkt?scrip-codes=... -> 5-level depth per code.

        NOTE: quantities and prices in the response are STRINGS, and
        quantities may contain thousands separators (e.g. "2,318") —
        strip commas before parsing to a number.
        """
        response = await self._http.get(
            "/market/quotes/mkt", params={"scrip-codes": ",".join(scrip_codes)}
        )
        response.raise_for_status()
        return response.json()["data"]

    async def get_historical(
        self, scrip_codes: list[str], interval: str, start_time_ms: int, end_time_ms: int
    ) -> dict[str, list[dict]]:
        """
        GET /market/historical/{interval} -> {scrip_code: [candles]}.

        Max 5 scrip_codes per call. start/end are epoch MILLISECONDS;
        each returned candle's own "ts" is epoch SECONDS (IST) — see
        module docs. Response wraps in a "success" boolean, not "status".
        """
        if len(scrip_codes) > 5:
            raise ValueError("At most 5 scrip_codes per historical data request")
        response = await self._http.get(
            f"/market/historical/{interval}",
            params={
                "scrip-codes": ",".join(scrip_codes),
                "start_time": start_time_ms,
                "end_time": end_time_ms,
            },
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("success"):
            raise RuntimeError(f"Historical data request failed: {body}")
        return {code: entry["candles"] for code, entry in body["data"].items()}
