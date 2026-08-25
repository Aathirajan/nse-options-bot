"""
scripts/capture_truedata_daily.py

Run this daily (e.g. via cron, after market close) to pull the latest
day's tick data from TrueData before it ages out of their default 5-day
retention window, and archive it into your own Parquet catalog. Over
time this builds up tick history beyond what TrueData retains on their
side.

Usage:
    python scripts/capture_truedata_daily.py --symbols NIFTY BANKNIFTY
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.data.catalog_wrangler import CatalogWrangler
from src.data.vendors.truedata_loader import TrueDataLoader

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", required=True, help="TrueData symbol strings")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--duration", default="1 D")
    args = parser.parse_args()

    with open(args.config) as f:
        settings = yaml.safe_load(f)

    import os

    loader = TrueDataLoader(
        username=os.environ.get("TRUEDATA_USERNAME", settings["truedata"]["username"]),
        password=os.environ.get("TRUEDATA_PASSWORD", settings["truedata"]["password"]),
    )
    wrangler = CatalogWrangler(settings["catalog"]["path"])

    results = loader.fetch_many(args.symbols, duration=args.duration)
    today = dt.date.today().isoformat()

    for symbol, ticks in results.items():
        if not ticks:
            logger.warning("No ticks captured for %s, skipping write", symbol)
            continue
        path = wrangler.write_ticks(ticks, partition_date=today)
        logger.info("Wrote %d ticks for %s -> %s", len(ticks), symbol, path)


if __name__ == "__main__":
    main()
