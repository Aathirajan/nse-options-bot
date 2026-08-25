"""
adapters/nautilus_factories.py

Config classes and factories needed to register IndstocksLiveMarketDataClient
and IndstocksLiveExecutionClient with a real nautilus_trader TradingNode.
This is the piece that ties everything else together — without it, the
two live client classes exist but the TradingNode has no way to
construct them.

Confirmed against nautilus_trader==1.231.0 (inspected directly):

    LiveDataClientConfig / LiveExecClientConfig are frozen msgspec
    Structs — subclass with `class X(LiveDataClientConfig, frozen=True)`
    and add fields as class-level annotations, same pattern real venue
    adapters (Binance, Bybit, etc.) use internally.

    LiveDataClientFactory.create(loop, name, config, msgbus, cache, clock)
        -> LiveDataClient
    LiveExecClientFactory.create(loop, name, config, msgbus, cache, clock)
        -> LiveExecutionClient

    TradingNode.add_data_client_factory(name, factory)
    TradingNode.add_exec_client_factory(name, factory)
"""

from __future__ import annotations

from nautilus_trader.live.config import LiveDataClientConfig, LiveExecClientConfig
from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory
from nautilus_trader.model.enums import AccountType, OmsType

from src.adapters.mapper import InstrumentMapper
from src.adapters.nautilus_instrument_provider import IndstocksInstrumentProvider
from src.adapters.nautilus_live_data_client import IndstocksLiveMarketDataClient
from src.adapters.nautilus_live_exec_client import IndstocksLiveExecutionClient


class IndstocksDataClientConfig(LiveDataClientConfig, frozen=True, kw_only=True):
    access_token: str
    instrument_master_path: str
    ws_url: str = "wss://ws-prices.indstocks.com/api/v1/ws/prices"
    initial_ws_tokens: tuple[str, ...] = ()


class IndstocksExecClientConfig(LiveExecClientConfig, frozen=True, kw_only=True):
    access_token: str
    instrument_master_path: str
    rest_base_url: str = "https://api.indstocks.com"
    ws_orders_url: str = "wss://ws-order-updates.indstocks.com/api/v1/ws/trades"
    dry_run: bool = True
    oms_type: str = "NETTING"
    account_type: str = "CASH"


class IndstocksLiveDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(loop, name, config: IndstocksDataClientConfig, msgbus, cache, clock):
        from nautilus_trader.model.identifiers import ClientId

        mapper = InstrumentMapper(config.instrument_master_path)
        provider = IndstocksInstrumentProvider(mapper)
        return IndstocksLiveMarketDataClient(
            loop=loop,
            client_id=ClientId(name),
            venue=None,  # multi-venue client (NSE + BSE instruments share one connection)
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            access_token=config.access_token,
            mapper=mapper,
            ws_url=config.ws_url,
            initial_ws_tokens=list(config.initial_ws_tokens),
        )


class IndstocksLiveExecClientFactory(LiveExecClientFactory):
    @staticmethod
    def create(loop, name, config: IndstocksExecClientConfig, msgbus, cache, clock):
        from nautilus_trader.model.identifiers import ClientId

        mapper = InstrumentMapper(config.instrument_master_path)
        provider = IndstocksInstrumentProvider(mapper)
        oms_type = OmsType[config.oms_type]
        account_type = AccountType[config.account_type]
        return IndstocksLiveExecutionClient(
            loop=loop,
            client_id=ClientId(name),
            venue=None,
            oms_type=oms_type,
            account_type=account_type,
            base_currency=None,
            instrument_provider=provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            access_token=config.access_token,
            mapper=mapper,
            rest_base_url=config.rest_base_url,
            ws_orders_url=config.ws_orders_url,
            dry_run=config.dry_run,
        )
