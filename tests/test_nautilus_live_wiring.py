"""
tests/test_nautilus_live_wiring.py

Proves the live-trading side of the Nautilus integration works:
IndstocksLiveMarketDataClient and IndstocksLiveExecutionClient can be
constructed against real nautilus_trader infrastructure (via its own
test_kit stubs), correctly resolve our internal instrument identifier
convention against Nautilus's real InstrumentId format, subscribe,
handle ticks, submit orders, process fills, and assemble into a full
TradingNode. No live network/credentials required — the WS/REST calls
themselves are never reached in these tests (a fake access token is
used, and _connect()/_disconnect() only start/stop the background
tasks, they don't block on a real handshake).

Run with:
    python tests/test_nautilus_live_wiring.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientId, InstrumentId, Venue
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.test_kit.stubs.component import TestComponentStubs

from src.adapters.indstocks_data import RawTick
from src.adapters.indstocks_exec import OrderUpdate
from src.adapters.mapper import InstrumentMapper
from src.adapters.nautilus_instrument_provider import IndstocksInstrumentProvider
from src.adapters.nautilus_live_data_client import IndstocksLiveMarketDataClient
from src.adapters.nautilus_live_exec_client import IndstocksLiveExecutionClient


def _run(coro):
    return asyncio.run(coro)


async def _make_provider() -> IndstocksInstrumentProvider:
    mapper = InstrumentMapper("config/instruments.json")
    provider = IndstocksInstrumentProvider(mapper)
    await provider.load_all_async()
    return provider, mapper


def test_data_client_subscribe_and_tick_handling():
    async def _inner():
        loop = asyncio.get_event_loop()
        msgbus, cache, clock = TestComponentStubs.msgbus(), TestComponentStubs.cache(), TestComponentStubs.clock()
        provider, mapper = await _make_provider()

        client = IndstocksLiveMarketDataClient(
            loop=loop, client_id=ClientId("INDSTOCKS"), venue=Venue("NSE"),
            msgbus=msgbus, cache=cache, clock=clock, instrument_provider=provider,
            access_token="FAKE", mapper=mapper,
        )

        from nautilus_trader.data.messages import SubscribeQuoteTicks

        cmd = SubscribeQuoteTicks(
            instrument_id=InstrumentId.from_str("NIFTY.NSE"),
            client_id=ClientId("INDSTOCKS"), venue=Venue("NSE"),
            command_id=UUID4(), ts_init=0,
        )
        await client._subscribe_quote_ticks(cmd)
        assert "NIDX:26000" in client._feed._subscribed_tokens

        tick = RawTick(
            ws_token="NIDX:26000", ltp=24500.0, timestamp_ns=1_755_000_000_000_000_000,
            bid=24499.5, ask=24500.5, bid_qty=100, ask_qty=150,
        )
        await client._on_raw_tick(tick)  # must not raise

    _run(_inner())


def test_exec_client_connect_submit_and_fill():
    async def _inner():
        loop = asyncio.get_event_loop()
        msgbus, cache, clock = TestComponentStubs.msgbus(), TestComponentStubs.cache(), TestComponentStubs.clock()
        provider, mapper = await _make_provider()

        client = IndstocksLiveExecutionClient(
            loop=loop, client_id=ClientId("INDSTOCKS"), venue=Venue("NSE"),
            oms_type=OmsType.NETTING, account_type=AccountType.CASH, base_currency=None,
            instrument_provider=provider, msgbus=msgbus, cache=cache, clock=clock,
            access_token="FAKE", mapper=mapper, dry_run=True,
        )
        await client._connect()
        assert client.account_id is not None

        of = TestComponentStubs.order_factory()
        order = of.limit(
            instrument_id=InstrumentId.from_str("NIFTY.NSE"), order_side=OrderSide.BUY,
            quantity=Quantity.from_int(1), price=Price.from_str("24500.00"),
            time_in_force=TimeInForce.DAY,
        )
        cmd = SubmitOrder(trader_id=of.trader_id, strategy_id=of.strategy_id, order=order, command_id=UUID4(), ts_init=0)
        await client._submit_order(cmd)
        assert len(client._order_context) == 1
        order_id = list(client._order_context.keys())[0]

        update = OrderUpdate(
            order_id=order_id, order_status="SUCCESS", filled_quantity=1, remaining_quantity=0,
            average_price=24500.0, timestamp_ns=1_755_000_000_000_000_000,
        )
        await client._on_order_update(update)  # must not raise

        await client._disconnect()

    _run(_inner())


def test_trading_node_assembles_with_indstocks_clients():
    os.environ.setdefault("INDSTOCKS_ACCESS_TOKEN", "FAKE")
    import scripts.run_live_nautilus as mod

    # Newer nautilus_trader refuses to auto-create an event loop when
    # running under pytest (to prevent loop leaks) -- create and pass
    # one explicitly instead of relying on TradingNode's internal
    # asyncio.get_event_loop() fallback.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    settings = mod.load_settings()
    settings = deepcopy(settings)
    settings["instruments"]["watchlist"] = ["NIFTY"]
    node_config = mod.build_node_config(settings, dry_run=True)
    node = mod.TradingNode(config=node_config, loop=loop)
    node.add_data_client_factory(mod.CLIENT_NAME, mod.IndstocksLiveDataClientFactory)
    node.add_exec_client_factory(mod.CLIENT_NAME, mod.IndstocksLiveExecClientFactory)
    node.build()

    strategy = mod.build_strategy(settings, "NIFTY-INDEX")
    node.trader.add_strategy(strategy)
    node.dispose()


def test_config_builds_all_enabled_strategies_with_one_arbiter():
    import scripts.run_live_nautilus as mod

    settings = deepcopy(mod.load_settings())
    settings["instruments"]["watchlist"] = ["NIFTY", "NIFTY"]
    settings["strategies"]["orpc"]["enabled"] = False
    arbiter = mod.build_arbiter(settings)
    strategies = mod.build_strategies(settings, arbiter)

    assert len(strategies) == 6
    assert {strategy._arbiter for strategy in strategies} == {arbiter}
    assert {strategy._inner.strategy_id for strategy in strategies} == {
        "obi_momentum_v1", "vwap_reversion_v1", "vol_breakout_v1",
    }

if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    passed, failed = 0, 0
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL: {test.__name__} -> {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
