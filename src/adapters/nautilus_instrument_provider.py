"""
adapters/nautilus_instrument_provider.py

Concrete nautilus_trader InstrumentProvider that loads instruments from
our own instrument master (config/instruments.json via InstrumentMapper)
instead of querying a venue API directly at startup — since our
"query" already happened offline via scripts/refresh_instruments.py.
"""

from __future__ import annotations

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.identifiers import InstrumentId

from src.adapters.instrument_builder import build_instrument
from src.adapters.mapper import InstrumentMapper


class IndstocksInstrumentProvider(InstrumentProvider):
    def __init__(self, mapper: InstrumentMapper, config=None):
        super().__init__(config=config)
        self._mapper = mapper

    async def load_all_async(self, filters: dict | None = None) -> None:
        for nautilus_id, record in self._mapper._by_nautilus_id.items():
            try:
                instrument = build_instrument(record)
            except ValueError:
                continue  # instrument_type we don't yet build (e.g. FUTCUR)
            self.add(instrument)

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        for instrument_id in instrument_ids:
            record = self._mapper.get_record(str(instrument_id))
            instrument = build_instrument(record)
            self.add(instrument)
