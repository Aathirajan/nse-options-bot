from __future__ import annotations

from unittest.mock import Mock

import pytest

from scripts.run_live import subscribe_orpc_tokens
from scripts.run_live_nautilus import build_node_config, build_watchlist_ws_tokens
from src.adapters.indstocks_data import IndstocksPriceFeed
from src.adapters.mapper import InstrumentMapper
from src.strategies.opening_dislocation import OpeningRangePremiumConvergence


def test_orpc_tokens_are_added_to_live_feed():
    feed = Mock(spec=IndstocksPriceFeed)
    strategy = OpeningRangePremiumConvergence(
        atm_ce_token="NFO:45108", atm_pe_token="NFO:45109", quantity=1
    )

    tokens = subscribe_orpc_tokens(feed, strategy)

    assert tokens == ["NFO:45108", "NFO:45109"]
    feed.subscribe.assert_called_once_with(tokens)


def test_nautilus_watchlist_resolves_all_configured_indices():
    settings = {
        "instruments": {"watchlist": ["NIFTY"]},
    }
    mapper = InstrumentMapper("config/instruments.json")

    assert build_watchlist_ws_tokens(settings, mapper) == ["NIDX:26000"]


def test_nautilus_watchlist_fails_without_confirmed_index_token():
    settings = {
        "instruments": {"watchlist": ["NIFTY", "SENSEX"]},
    }
    mapper = InstrumentMapper("config/instruments.json")

    with pytest.raises(RuntimeError, match="SENSEX"):
        build_watchlist_ws_tokens(settings, mapper)
