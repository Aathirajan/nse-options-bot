"""
data/catalog_wrangler.py

The seam between "whatever format a data source ships" and Nautilus's
ParquetDataCatalog. Both the TrueData vendor loader and a live INDstocks
WS recorder should funnel through NormalizedTick -> this module, so
scripts/run_backtest.py reads one consistent shape regardless of where
the ticks originally came from.

TODO: the actual nautilus_trader.persistence.catalog.ParquetDataCatalog
write path (write_data / _write_chunk, exact QuoteTick/TradeTick
constructor signature) varies across nautilus_trader versions. Confirm
against your pinned version's docs/examples before relying on
write_to_nautilus_catalog(); the NormalizedTick dataclass and the
to_dataframe()/to_parquet() path below work standalone today and are a
safe intermediate format regardless.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class NormalizedTick:
    """Source-agnostic tick shape. Every vendor loader and the live
    recorder should produce rows in this shape before writing out."""

    nautilus_instrument_id: str
    timestamp_ns: int
    price: float
    volume: int
    bid: float | None = None
    ask: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    source: str = "unknown"  # "truedata" | "indstocks_live" | ...


class CatalogWrangler:
    def __init__(self, catalog_path: str | Path):
        self.catalog_path = Path(catalog_path)
        self.catalog_path.mkdir(parents=True, exist_ok=True)

    def write_ticks(
        self,
        ticks: list[NormalizedTick],
        *,
        partition_date: str,  # "2026-08-21", used for the output filename
    ) -> Path:
        """
        Writes normalized ticks to a Parquet file, partitioned by
        instrument and date — a durable, source-agnostic intermediate
        format you can inspect with pandas even before wiring up the
        actual Nautilus ParquetDataCatalog ingestion.
        """
        if not ticks:
            raise ValueError("No ticks to write")

        df = pd.DataFrame([asdict(t) for t in ticks])
        df = df.sort_values("timestamp_ns").reset_index(drop=True)

        instrument_id = ticks[0].nautilus_instrument_id
        safe_id = instrument_id.replace("/", "_")
        out_dir = self.catalog_path / "raw_ticks" / safe_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{partition_date}.parquet"

        df.to_parquet(out_path, index=False)
        return out_path

    def read_ticks(
        self,
        nautilus_instrument_id: str,
        partition_date: str,
    ) -> pd.DataFrame:
        safe_id = nautilus_instrument_id.replace("/", "_")
        path = self.catalog_path / "raw_ticks" / safe_id / f"{partition_date}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"No recorded ticks at {path}")
        return pd.read_parquet(path)

    def write_to_nautilus_catalog(
        self,
        ticks: list[NormalizedTick],
        instrument,  # a nautilus_trader Instrument, e.g. from instrument_builder.build_instrument()
    ) -> None:
        """
        Converts NormalizedTick rows into real nautilus_trader QuoteTick
        objects and writes them via ParquetDataCatalog.write_data —
        confirmed against nautilus_trader==1.231.0's actual constructor
        signatures (inspected directly):

            ParquetDataCatalog(path, ...)
            catalog.write_data(data: list, ...)
            QuoteTick(instrument_id, bid_price, ask_price, bid_size,
                      ask_size, ts_event, ts_init)

        Ticks missing bid/ask (bare LTP-only ticks) fall back to using
        `price` for both sides with a nominal 1-unit size, since
        QuoteTick requires all four fields — this is a reasonable
        placeholder for LTP-only data but means spread-dependent
        strategies should prefer real quote-mode captures over LTP-only
        history where possible.

        `instrument` must already be registered with your BacktestEngine
        (via add_instrument()) with the SAME instrument_id these ticks
        are tagged with, or the engine won't route them anywhere.
        """
        from nautilus_trader.model.data import QuoteTick
        from nautilus_trader.model.objects import Price, Quantity
        from nautilus_trader.persistence.catalog import ParquetDataCatalog

        if not ticks:
            raise ValueError("No ticks to write")

        price_precision = instrument.price_precision
        quote_ticks = []
        for t in sorted(ticks, key=lambda x: x.timestamp_ns):
            bid = t.bid if t.bid is not None else t.price
            ask = t.ask if t.ask is not None else t.price
            bid_size = t.bid_size if t.bid_size is not None else 1
            ask_size = t.ask_size if t.ask_size is not None else 1

            quote_ticks.append(
                QuoteTick(
                    instrument_id=instrument.id,
                    bid_price=Price(bid, price_precision),
                    ask_price=Price(ask, price_precision),
                    bid_size=Quantity.from_int(int(bid_size)),
                    ask_size=Quantity.from_int(int(ask_size)),
                    ts_event=t.timestamp_ns,
                    ts_init=t.timestamp_ns,
                )
            )

        catalog = ParquetDataCatalog(str(self.catalog_path / "nautilus"))
        catalog.write_data(quote_ticks)
