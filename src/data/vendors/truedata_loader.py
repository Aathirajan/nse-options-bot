"""
data/vendors/truedata_loader.py

Wraps TrueData's TD_hist client, pulls historical tick data, and
normalizes it into NormalizedTick rows for the catalog wrangler.

NOTE on retention: TrueData's historical tick data via REST defaults to
the last 5 trading days unless your account has an extended-history
add-on (confirm with TrueData support/sales) — see settings.yaml's
`truedata.tick_history_days_available`. For your fast-scalp strategies
(ORPC, OBI momentum) you'll likely want a scheduled daily job that pulls
each day's ticks before they age out, rather than relying on TrueData as
an infinite on-demand backfill. scripts/capture_truedata_daily.py sets
this up as a cron-friendly script.

Symbol mapping: TrueData's symbol format does NOT match INDstocks'
token format or your instruments.json — you need a separate mapping,
since TrueData indexes by their own symbol strings (e.g. "NIFTY-I" for
continuous futures, or literal option symbols). Populate
TRUEDATA_SYMBOL_MAP below (or better, generate it alongside
instruments.json) before relying on this for anything beyond ad-hoc
testing.
"""

from __future__ import annotations

import logging

from src.data.catalog_wrangler import NormalizedTick

logger = logging.getLogger(__name__)

# TODO: populate/generate this alongside config/instruments.json.
# Key: TrueData symbol string. Value: your Nautilus instrument id.
TRUEDATA_SYMBOL_MAP: dict[str, str] = {
    "NIFTY": "NIFTY-INDEX",
    # options and SENSEX need their own literal symbol strings from
    # TrueData's symbol master — fetch and cache that master rather than
    # hand-maintaining it, since it changes as contracts roll.
}


class TrueDataLoader:
    def __init__(self, username: str, password: str):
        self._username = username
        self._password = password
        self._td_hist = None  # lazy import + init, see _client()

    def _client(self):
        if self._td_hist is None:
            try:
                from truedata import TD_hist
            except ImportError as exc:
                raise ImportError(
                    "Install the 'truedata' package (pip install truedata) "
                    "to use TrueDataLoader."
                ) from exc
            self._td_hist = TD_hist(self._username, self._password)
        return self._td_hist

    def fetch_ticks(
        self,
        truedata_symbol: str,
        duration: str = "5 D",
        bidask: bool = True,
    ) -> list[NormalizedTick]:
        """
        Pulls tick-level history for one symbol and normalizes it.

        Rate limits (per TrueData docs): 5 requests/sec, 300/min,
        18,000/hour for tick data — if you're looping over an option
        chain, throttle accordingly (see fetch_many below).
        """
        nautilus_id = TRUEDATA_SYMBOL_MAP.get(truedata_symbol)
        if nautilus_id is None:
            raise KeyError(
                f"No TRUEDATA_SYMBOL_MAP entry for '{truedata_symbol}' — "
                f"add it before fetching."
            )

        client = self._client()
        raw_df = client.get_historic_data(
            truedata_symbol, duration=duration, bar_size="ticks", bidask=bidask
        )

        ticks: list[NormalizedTick] = []
        for row in raw_df.itertuples():
            ticks.append(
                NormalizedTick(
                    nautilus_instrument_id=nautilus_id,
                    timestamp_ns=int(row.Index.timestamp() * 1e9),
                    price=float(row.ltp) if hasattr(row, "ltp") else float(row.close),
                    volume=int(getattr(row, "volume", 0) or 0),
                    bid=float(getattr(row, "bid", None)) if hasattr(row, "bid") else None,
                    ask=float(getattr(row, "ask", None)) if hasattr(row, "ask") else None,
                    bid_size=int(getattr(row, "bid_qty", 0) or 0) if hasattr(row, "bid_qty") else None,
                    ask_size=int(getattr(row, "ask_qty", 0) or 0) if hasattr(row, "ask_qty") else None,
                    source="truedata",
                )
            )
        logger.info("Fetched %d ticks for %s", len(ticks), truedata_symbol)
        return ticks

    def fetch_many(
        self,
        truedata_symbols: list[str],
        duration: str = "5 D",
        throttle_seconds: float = 0.25,  # keeps you under 5 req/sec comfortably
    ) -> dict[str, list[NormalizedTick]]:
        import time

        results: dict[str, list[NormalizedTick]] = {}
        for symbol in truedata_symbols:
            try:
                results[symbol] = self.fetch_ticks(symbol, duration=duration)
            except Exception as exc:  # noqa: BLE001 — log and continue the batch
                logger.error("Failed to fetch %s: %s", symbol, exc)
                results[symbol] = []
            time.sleep(throttle_seconds)
        return results
