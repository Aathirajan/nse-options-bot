from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.messages import SubscribeQuoteTicks
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Venue
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from src.adapters.indstocks_data import IndstocksPriceFeed, QuoteMessage
from src.adapters.instrument_builder import build_instrument
from src.adapters.mapper import InstrumentMapper
from src.adapters.nautilus_live_data_client import IndstocksLiveMarketDataClient
from src.adapters.nautilus_instrument_provider import IndstocksInstrumentProvider

FIXTURE = Path(__file__).parent / "fixtures" / "quote_sample.jsonl"


def _payloads() -> list[dict]:
    payloads = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            payload = json.loads(line)
            payloads.append(json.loads(payload) if isinstance(payload, str) else payload)
    return payloads


def test_quote_fixture_replays_to_raw_ticks():
    feed = IndstocksPriceFeed(access_token="FAKE")
    feed.subscribe(["NFO:61593", "NFO:61734"])

    ticks = [feed._parse_tick(payload) for payload in _payloads()]

    assert all(tick is not None for tick in ticks)
    assert [tick.ltp for tick in ticks] == [311.3, 7.05, 309.9]
    assert [tick.volume for tick in ticks] == [2271685, 42348215, 2291770]
    assert all(tick.bid is None and tick.ask is None for tick in ticks)


def test_quote_schema_raises_for_missing_required_field():
    payload = _payloads()[0]
    del payload["data"]["volume"]

    with pytest.raises(ValueError, match="volume"):
        QuoteMessage.from_payload(payload)


def test_fixture_quote_builds_nautilus_quote_ticks():
    async def run_test():
        mapper = InstrumentMapper("config/instruments.json")
        loop = asyncio.get_running_loop()
        msgbus = TestComponentStubs.msgbus()
        cache = TestComponentStubs.cache()
        clock = TestComponentStubs.clock()
        provider = IndstocksInstrumentProvider(mapper)
        await provider.load_all_async()
        client = IndstocksLiveMarketDataClient(
            loop=loop,
            client_id=ClientId("INDSTOCKS"),
            venue=Venue("NSE"),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            access_token="FAKE",
            mapper=mapper,
        )
        record = mapper.from_ws_token_string("NFO:61593")
        assert record is not None
        instrument = build_instrument(record, ts_now_ns=1)
        command = SubscribeQuoteTicks(
            instrument_id=instrument.id,
            client_id=ClientId("INDSTOCKS"),
            venue=Venue("NSE"),
            command_id=UUID4(),
            ts_init=0,
        )
        await client._subscribe_quote_ticks(command)
        received = []
        client._handle_data = received.append

        feed = IndstocksPriceFeed(access_token="FAKE")
        feed.subscribe(["NFO:61593"])
        tick = feed._parse_tick(_payloads()[0])
        assert tick is not None
        await client._on_raw_tick(tick)

        assert len(received) == 1
        quote_tick = received[0]
        assert quote_tick.instrument_id == instrument.id
        assert quote_tick.bid_price.as_double() == 311.3
        assert quote_tick.ask_price.as_double() == 311.3
        assert quote_tick.bid_size.as_double() == 1
        assert quote_tick.ask_size.as_double() == 1

    asyncio.run(run_test())
