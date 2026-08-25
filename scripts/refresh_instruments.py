"""
scripts/refresh_instruments.py

Downloads the real instrument master from INDstocks (GET /market/instruments
for source=index, source=equity, source=fno) and regenerates
config/instruments.json in the schema InstrumentMapper expects.

CSV structures, confirmed against https://api-docs.indstocks.com/instruments/:

  source=index  -> 3 columns: EXCH, SEGMENT (actually holds the index NAME,
                   not a segment code — read positionally), SECURITY_ID
  source=equity, source=fno -> 16 columns: EXCH, SEGMENT, SECURITY_ID,
                   INSTRUMENT_NAME, EXPIRY_CODE, TRADING_SYMBOL, LOT_UNITS,
                   CUSTOM_SYMBOL, EXPIRY_DATE, STRIKE_PRICE, OPTION_TYPE,
                   TICK_SIZE, EXPIRY_FLAG, SEM_EXCH_INSTRUMENT_TYPE,
                   SERIES, SYMBOL_NAME

Usage:
    export INDSTOCKS_ACCESS_TOKEN=...
    python scripts/refresh_instruments.py --watchlist NIFTY SENSEX
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REST_BASE_URL = "https://api.indstocks.com"


def _normalize_symbol(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def _canonical_index_symbol(index_name: str, watchlist: list[str]) -> str | None:
    normalized_name = _normalize_symbol(index_name)
    # INDstocks calls the NIFTY 50 index "NIFTY 50", while the local
    # watchlist keeps the shorter trading name "NIFTY".
    aliases = {"NIFTY": {"NIFTY50"}}
    for symbol in watchlist:
        canonical_symbol = _normalize_symbol(symbol)
        if normalized_name == canonical_symbol or normalized_name in aliases.get(canonical_symbol, set()):
            return canonical_symbol
    return None


def _fno_underlying(row: dict) -> str:
    symbol_name = (row.get("SYMBOL_NAME") or "").strip()
    if symbol_name:
        return symbol_name.upper()
    trading_symbol = (row.get("TRADING_SYMBOL") or "").strip()
    return trading_symbol.split("-", 1)[0].upper()


def _parse_expiry(value: str) -> str | None:
    value = value.strip()
    for format_string in ("%m/%d/%Y %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, format_string).date().isoformat()
        except ValueError:
            continue
    return value[:10] if value else None


def fetch_csv(client: httpx.Client, source: str) -> list[dict]:
    response = client.get("/market/instruments", params={"source": source})
    response.raise_for_status()
    text = response.text

    if source == "index":
        reader = csv.reader(io.StringIO(text))
        rows = []
        for row in reader:
            if not row or row[0] == "EXCH":  # skip header
                continue
            rows.append({"exch": row[0], "index_name": row[1], "security_id": row[2]})
        return rows

    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def build_index_records(rows: list[dict], watchlist: list[str], ws_overrides: dict[str, str] | None = None) -> list[dict]:
    """
    Index CSV's second column is labeled SEGMENT but actually holds the
    index name (per the docs' explicit warning) — match against the
    watchlist by that name, normalizing whitespace.

    ws_overrides: optional {symbol: ws_token} map for the NIDX:/BIDX:
    WS subscribe token, which is NOT derivable from this CSV's
    security_id — see mapper.py's module docstring. Without an entry
    here, the record gets ws_token_override=None and mapper.py will
    raise if you try to subscribe to it on the WS feed until you
    capture and add the real token (scripts/capture_ws_payload.py).
    """
    ws_overrides = ws_overrides or {}
    records = []
    for row in rows:
        symbol = _canonical_index_symbol(row["index_name"], watchlist)
        if symbol is None:
            continue
        records.append(
            {
                "security_id": row["security_id"],
                "exchange": row["exch"],
                "nautilus_id": f"{symbol}-INDEX",
                "symbol": symbol,
                "instrument_type": "INDEX",
                "tick_size": 0.05,
                "ws_token_override": ws_overrides.get(symbol),
                "lot_size": 1,
            }
        )
    return records


def build_fno_records(rows: list[dict], watchlist: list[str]) -> list[dict]:
    from src.adapters.mapper import InstrumentMapper

    records = []
    for row in rows:
        symbol_name = _fno_underlying(row)
        normalized_symbol = _normalize_symbol(symbol_name)
        if not any(normalized_symbol.startswith(_normalize_symbol(w)) for w in watchlist):
            continue
        option_type = row.get("OPTION_TYPE") or None
        strike_raw = row.get("STRIKE_PRICE") or ""
        expiry_raw = row.get("EXPIRY_DATE") or ""

        try:
            strike = float(strike_raw) if strike_raw else None
        except ValueError:
            strike = None

        expiry_iso = _parse_expiry(expiry_raw) if expiry_raw else None

        nautilus_id = row.get("TRADING_SYMBOL") or row.get("CUSTOM_SYMBOL") or symbol_name
        if option_type and strike and expiry_iso:
            try:
                nautilus_id = InstrumentMapper.build_nautilus_id(
                    symbol=symbol_name,
                    expiry=expiry_iso,
                    strike=strike,
                    option_type=option_type,
                    exchange="NFO" if row.get("EXCH") == "NSE" else "BFO",
                )
            except Exception:  # noqa: BLE001 — fall back to raw trading symbol
                logger.warning(
                    "Could not build nautilus_id for %s, using trading_symbol fallback",
                    row.get("SECURITY_ID"),
                )

        try:
            tick_size = float(row.get("TICK_SIZE") or 0.05)
        except ValueError:
            tick_size = 0.05
        try:
            lot_size = int(float(row.get("LOT_UNITS") or 1))
        except ValueError:
            lot_size = 1

        records.append(
            {
                "security_id": row.get("SECURITY_ID"),
                "exchange": row.get("EXCH"),
                "nautilus_id": nautilus_id,
                "symbol": symbol_name,
                "instrument_type": "OPTION"
                if (row.get("INSTRUMENT_NAME") or "").upper().startswith("OPT")
                else row.get("INSTRUMENT_NAME") or "OPTION",
                "option_type": option_type,
                "strike": strike,
                "expiry": expiry_iso,
                "tick_size": tick_size,
                "lot_size": lot_size,
            }
        )
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist", nargs="+", default=["NIFTY", "SENSEX"])
    parser.add_argument("--out", default="config/instruments.json")
    parser.add_argument("--include-fno", action="store_true", help="Also fetch F&O contracts (large file)")
    args = parser.parse_args()

    token = os.environ.get("INDSTOCKS_ACCESS_TOKEN")
    if not token:
        raise SystemExit("Set INDSTOCKS_ACCESS_TOKEN before running")

    with httpx.Client(base_url=REST_BASE_URL, headers={"Authorization": token}, timeout=30.0) as client:
        logger.info("Fetching index instruments...")
        index_rows = fetch_csv(client, "index")
        # WS index tokens are NOT derivable from this CSV — see
        # mapper.py's module docstring. NIFTY's is INDstocks' own docs
        # example (unconfirmed to specifically be NIFTY); others are
        # unknown until captured via scripts/capture_ws_payload.py.
        ws_overrides = {"NIFTY50": "NIDX:26000", "NIFTY": "NIDX:26000"}
        records = build_index_records(index_rows, args.watchlist, ws_overrides)
        logger.info("Matched %d index instruments", len(records))

        if args.include_fno:
            logger.info("Fetching F&O instruments (this file is large)...")
            fno_rows = fetch_csv(client, "fno")
            fno_records = build_fno_records(fno_rows, args.watchlist)
            logger.info("Matched %d F&O instruments", len(fno_records))
            records.extend(fno_records)

    output = {
        "_comment": "Auto-generated by scripts/refresh_instruments.py — do not hand-edit",
        "instruments": records,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(output, indent=2))
    logger.info("Wrote %d instruments to %s", len(records), out_path)


if __name__ == "__main__":
    main()
