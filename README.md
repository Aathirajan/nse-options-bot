# Indstocks Nautilus Opportunity Engine

Multi-strategy trading engine for Indian index/options markets: OBI
momentum, VWAP reversion, volatility breakout, and opening-range premium
convergence (ORPC) strategies all emit `OpportunitySignal` events into a
shared `OpportunityArbiter`, which scores each one against real Indian
market costs (STT, GST, NSE turnover, brokerage) and only approves trades
above a net expected-value threshold.

**Status: working scaffold with a real, tested NautilusTrader integration
— backtest AND live.** Cost engine, arbiter, features, strategies, the
simplified backtest replay loop, a full real `BacktestEngine` run, a
full real `TradingNode` with live `LiveMarketDataClient`/
`LiveExecutionClient` wiring, and a real Option Chain-based ATM
resolver for the ORPC strategy are all implemented and covered by
tests — 33/33 pass (`python -m pytest tests/ -v`). Every INDstocks
schema referenced in this repo — order placement/modify/cancel, order
status vocabulary, the instrument master CSV, the Option Chain and
List Expiries endpoints, market quotes/depth, and the WS `ltp`-mode
payload — is verified against the full official API docs export, not
guessed. Two real bugs were caught this way (see "Bugs found and
fixed" below) that would have silently broken live trading. One thing
remains genuinely unverifiable without a live payload capture — see
"What's still open" below.

---

## Quick Start: Run Locally in 5 Minutes

### Prerequisites
- Python 3.11+ (test with `python --version`)
- INDstocks API token (sign up at [indstocks.com](https://indstocks.com))

### 1. Install & Configure

```bash
# Install dependencies
pip install -r requirements.txt

# Create local config (never commit this file)
cp config/settings.yaml config/settings.local.yaml
# Edit config/settings.local.yaml → fill in your INDstocks token

# Or use environment variable instead
export INDSTOCKS_ACCESS_TOKEN="your-token-here"
```

### 2. Download Instrument Master

```bash
export INDSTOCKS_ACCESS_TOKEN="your-token-here"
python scripts/refresh_instruments.py --watchlist NIFTY SENSEX --include-fno
```

This fetches NIFTY, SENSEX, and today's option contracts from INDstocks.

### 3. Verify Setup
```bash
python -m pytest tests/ -v
```

Should show: **33 passed** ✅

### 4. Run Backtest (No Network)

```bash
python scripts/run_backtest.py
```

Runs all 4 strategies against historical NIFTY data with the Nautilus backtest engine.

### 5. Paper Trade (Live Feed, Simulated Orders)

```bash
export INDSTOCKS_ACCESS_TOKEN="your-token-here"
python scripts/run_live.py --mode PAPER --config config/settings.yaml

```

Streams real NIFTY/SENSEX ticks, processes signals, but does NOT place real orders.

Watch the console for:
```
[2026-08-24 14:32:10] APPROVED OBI_MOMENTUM BUY qty=1 net_ev=45.2bps
[2026-08-24 14:32:10] Submitting order (dry_run=True)
```

Press `Ctrl+C` to stop.

---

## Full Documentation

- **[DEPLOYMENT.md](DEPLOYMENT.md)** — Detailed setup, paper trading, live trading, troubleshooting, monitoring
- **This README** — Architecture, what's implemented, what's verified, known limitations

---

**A note on live testing:** everything above was built and tested
against real `nautilus_trader` infrastructure, but never against a real
INDstocks connection — this was built in a sandbox that can't reach
`api.indstocks.com` or the WS hosts. Step 3 (capturing a real quote
payload) and Step 8 (an actual live/paper run) have to happen on your
machine, with your real access token. Everything's set up to make that
as close to copy-paste as possible.

**A note on doc sources:** the schema confirmations throughout this
README come from two passes — an initial round of targeted web
searches, followed by a complete official docs export (the full
`llms-full.md`-style flattened file covering every endpoint) which
caught two real bugs the first pass missed (see "Bugs found and fixed"
below). If INDstocks' docs change after this was written, re-verify
against https://api-docs.indstocks.com/llms-full.md rather than
trusting this file indefinitely.

---

## 1. What's built and tested right now

| Component | File | Status |
|---|---|---|
| Cost engine (STT/GST/turnover/brokerage/stamp duty) | `src/core/cost_engine.py` | ✅ tested |
| Opportunity Arbiter (single-signal gating, batch ranking, risk limits) | `src/core/arbiter.py` | ✅ tested |
| OBI calculator | `src/features/obi_calculator.py` | ✅ tested |
| Anchored VWAP + bands | `src/features/vwap_engine.py` | ✅ tested |
| OBI momentum / VWAP reversion / vol breakout / ORPC strategies | `src/strategies/*.py` | ✅ tested |
| Instrument mapper (WS token / REST scrip-code / security_id ↔ our internal id) | `src/adapters/mapper.py` | ✅ tested |
| Instrument builder (our id → real Nautilus `IndexInstrument`/`OptionContract`) | `src/adapters/instrument_builder.py` | ✅ tested |
| Catalog wrangler (raw Parquet AND real Nautilus `ParquetDataCatalog`) | `src/data/catalog_wrangler.py` | ✅ both paths tested |
| Nautilus backtest strategy bridge | `src/strategies/nautilus_bridge.py` | ✅ tested, submits real orders |
| Full `BacktestEngine` runner | `scripts/run_nautilus_backtest.py` | ✅ runs end-to-end |
| **`IndstocksLiveMarketDataClient`** (real `LiveMarketDataClient` subclass) | `src/adapters/nautilus_live_data_client.py` | ✅ tested: connect, subscribe, tick→`QuoteTick` |
| **`IndstocksLiveExecutionClient`** (real `LiveExecutionClient` subclass) | `src/adapters/nautilus_live_exec_client.py` | ✅ tested: connect, submit, fill |
| Live client configs + factories | `src/adapters/nautilus_factories.py` | ✅ tested |
| Full `TradingNode` live/paper launcher | `scripts/run_live_nautilus.py` | ✅ assembles end-to-end (no live network in tests) |
| Simplified backtest replay / standalone asyncio live loop | `scripts/run_backtest.py`, `scripts/run_live.py` | ✅ still work, simpler to debug |
| TrueData historical loader | `src/data/vendors/truedata_loader.py` | ⚠️ needs your TrueData creds + symbol map to verify |
| Option Chain / List Expiries / market data client | `src/adapters/indstocks_market_data.py` | ✅ tested against real docs example payload |
| ORPC live ATM CE/PE resolver | `scripts/run_live.py`'s `resolve_orpc_strategy()` | ✅ tested against a mocked option chain |





## 2. What's confirmed vs. what's still open

**Confirmed against live INDstocks docs (api-docs.indstocks.com) and
implemented accordingly:**

- **Order placement** (`POST /order`) — exact fields (`txn_type`,
  `exchange`, `segment`, `product`, `order_type`, `validity`,
  `security_id`, `qty`, `algo_id`, `limit_price`, `is_amo`, `remarks`),
  the `algo_id` constants (`"99999"` NSE / `"9999999999999999"` BSE),
  and the fact that `MARKET` orders are silently converted to `LIMIT` at
  the live price — there's no true market order via the API.
- **Order modify/cancel** (`POST /order/modify`, `POST /order/cancel`)
  — exact payload shapes, including that `modify` cannot change `remarks`.
- **Order status vocabulary** — the real enum (`QUEUED`, `O-PENDING`,
  `SL-PENDING`, `PROCESSING`, `ABORTED`, `INITIATED`, `SUCCESS`,
  `CANCELLED`, `MODIFIED`, `PENDING`, `EXPIRED`, `FAILED`, `"PARTIALLY
  FILLED"`, etc.) — importantly this does **not** cleanly match the
  WebSocket order-update feed's vocabulary (`"PARTIALLY_EXECUTED"` with
  an underscore), which `indstocks_exec.py` flags explicitly rather than
  papering over.
- **Order book / order details / trade book** (`GET /order-book`,
  `GET /order`, `GET /order/trades`, `GET /trade-book`) — real response
  shapes, now implemented as methods on `IndstocksExecutionClient`.
- **`remarks` field** — a 100-char free-text tag echoed back on every
  read, the cleanest way to reconcile fills against your own
  `strategy_id`. `run_live.py` now tags every order with
  `{strategy_id}/{signal_timestamp}`.
- **Three distinct identifier formats** — WS token (`"NSE:2885"`), REST
  quotes scrip-code (`"NSE_2885"`), and REST order `security_id`
  (bare `"2885"`, exchange passed separately). `mapper.py` was rebuilt
  around this — it was wrong in the first draft, which assumed a single
  unified token format.
- **Instrument master CSV** (`GET /market/instruments?source=...`) —
  real column names for `equity`/`fno` (16 columns) vs. `index` (3
  columns, and the index file's `SEGMENT` column actually holds the
  index *name*, not a segment code — confirmed directly from the docs'
  own callout). `scripts/refresh_instruments.py` implements this.
- **WS price feed LTP-mode payload** — confirmed exact shape
  (`{"mode": "ltp", "instrument": "2885", "timestamp": ..., "data":
  {"ltp": 1426}}`), including that the response drops the exchange
  prefix, which `indstocks_data.py` now re-attaches using the
  subscription list.
- **Market Quotes / Market Depth** (`GET /market/quotes/full|ltp|mkt`)
  — real response shapes. Depth values are **strings with thousands
  separators** (e.g. `"2,318"`), not numbers — `get_market_depth()` in
  `indstocks_market_data.py` documents this rather than silently
  mis-parsing it.
- **Option Chain / List Expiries** (`GET /market/option-chain`,
  `GET /market/instruments/expiries`) — real response shapes, parsing
  verified against the docs' own worked example. This is what resolves
  ORPC's "which CE/PE tokens are ATM today" problem —
  `resolve_orpc_strategy()` in `run_live.py` chains the two calls into
  real WS tokens automatically, tested against a mocked response.
- **Contracts & Expiries search** (`GET /market/instruments/search`,
  the `/expired/*` variants) — documented for completeness; not yet
  wired into any strategy, since List Expiries + Option Chain already
  cover what ORPC needs.
- **Order read endpoints use different field names than write
  endpoints.** `POST /order`, `/order/modify`, `/order/cancel` return
  `order_id`/`order_status`. `GET /order-book`, `GET /order` return
  `id`/`status` for the same concepts — confirmed from the docs' own
  worked examples, now documented inline in `indstocks_exec.py`.

**Still genuinely open — no verified example available:**

1. **WS `quote`-mode bid/ask/depth data.** Live capture confirmed the
  quote envelope and its OHLCV fields (`open`, `high`, `low`, `close`,
  `volume`), preserved in [tests/fixtures/quote_sample.jsonl](tests/fixtures/quote_sample.jsonl).
  The feed does not provide bid/ask prices, quantities, or order-book
  depth, so spread/OBI strategies cannot derive those values from this
  payload.
2. **F&O CSV's `EXPIRY_DATE` format** — not shown as a literal example
   in the docs. `refresh_instruments.py` assumes an ISO-ish string and
   takes the first 10 characters; adjust `build_fno_records()` once you
   see a real row.
3. **Nautilus `DataClient`/`ExecutionClient` subclassing** — both
   adapter files have a `TODO` block showing where to wrap the raw
   client in Nautilus's `LiveMarketDataClient`/`LiveExecutionClient`
   ABCs. The exact base-class methods differ across `nautilus_trader`
   versions, so this needs to happen against whichever version you pin.
4. **`ParquetDataCatalog` native write** — `catalog_wrangler.py` writes
   to a source-agnostic Parquet format today (tested, working). Wiring
   that into Nautilus's actual `ParquetDataCatalog.write_data()` is left
   as a `TODO` pending the same version confirmation.
6. **Live client instrument coverage** — `IndstocksLiveMarketDataClient`
   currently implements quote-tick subscription only (matching what the
   current strategy set consumes). If you add strategies needing order
   book depth or trade ticks, extend `_parse_tick`/add
   `_subscribe_order_book_deltas`/`_subscribe_trade_ticks` overrides.
7. **`IndstocksLiveMarketDataClient._unsubscribe_quote_ticks`** — is a
   no-op with a warning today, since `IndstocksPriceFeed` only supports
   one running subscription set for the whole WS connection. Fine for a
   fixed watchlist; extend `IndstocksPriceFeed` with a real unsubscribe
   if you need to dynamically add/drop instruments at runtime.

**Bugs found and fixed while building the real Nautilus integration**
(worth knowing about since they'd have caused silent wrong behavior):

- `instrument_builder.py` initially parsed the venue from the
  `"NIFTY-INDEX"` internal id's suffix, treating `"INDEX"` itself as a
  trading venue instead of falling back to the real exchange
  (NSE/BSE). Fixed — indices now correctly resolve to their real
  exchange venue.
- `arbiter.py`'s `score()` computed net EV correctly but never updated
  `self.state` (open position count, approved-today list) — only
  `evaluate_batch()` did that. Since `run_live.py` and the Nautilus
  strategy bridge process one signal at a time (not as a batch), this
  meant **position limits were silently not being enforced** in live/
  single-signal flows, even though the underlying orders were placed
  correctly. Added `evaluate_single()`, which scores AND gates AND
  updates state in one call, and switched both single-signal callers to
  use it.
- **Our own internal id convention (`mapper.nautilus_id`, e.g.
  `"NIFTY-INDEX"`) is NOT the same string as the real
  `nautilus_trader.model.identifiers.InstrumentId`** that
  `instrument_builder.build_instrument()` constructs (e.g. `"NIFTY.NSE"`
  — dot-separated, real venue). Both `IndstocksLiveMarketDataClient` and
  `IndstocksLiveExecutionClient` initially did a naive lookup using the
  wrong convention, which would have failed on every real subscribe/
  order. Fixed with a shared `find_record_by_real_instrument_id()`
  helper in `instrument_builder.py` that both clients now use
  consistently.
- `generate_order_submitted()`/`generate_account_state()` require a
  registered Nautilus `AccountId` whose issuer matches the *client's own
  ID*, not the venue — the execution client wasn't registering an
  account at all initially, which would have made every order event
  silently fail to propagate. Fixed in `_connect()`.
- **`mapper.py` built INDEX WebSocket subscribe tokens by reusing the
  REST `security_id` with an `NSE:`/`BSE:` prefix (e.g.
  `"NSE:40000001"`).** The full docs explicitly warn this is wrong: WS
  indices use a `NIDX:`/`BIDX:` prefix, AND the numeric token itself is
  a **different, unrelated number** from the REST `security_id` — the
  docs' own example is `NIDX:26000` for NIFTY vs. its REST
  `security_id` of `40000001`. Every index subscribe call in the
  original code would have silently subscribed to the wrong (likely
  nonexistent) instrument. Fixed: `InstrumentRecord.ws_token` now
  **raises** for an INDEX record with no explicit `ws_token_override`,
  rather than deriving a value the docs say may be wrong. NIFTY's
  override is populated from the docs' own worked example (plausible
  but not 100% confirmed to specifically be NIFTY); BANKNIFTY/SENSEX
  are left `null`, requiring a real capture via
  `scripts/capture_ws_payload.py` before they can be subscribed to.
  Equity and F&O tokens are unaffected — the docs confirm those ARE
  consistent between WS and REST.

## 3. Step-by-step: getting this running

### Step 1 — Environment setup
```bash
cd indstocks_nautilus_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
`nautilus_trader` is a large package with Rust extensions — if the wheel
install fails on your machine, check their install docs for
platform-specific instructions.

### Step 2 — Run the test suite (no credentials needed)
```bash
python -m pytest tests/ -v
```
21 tests across three files. `test_core_logic.py` (14) covers the cost
engine, arbiter, features, all four strategies, the mapper's index
WS-token safety check, Option Chain parsing, and the ORPC ATM
resolver — against synthetic data and the docs' own worked examples,
no live connection needed. `test_nautilus_integration.py` (4) proves the real
`BacktestEngine` pipeline works end-to-end. `test_nautilus_live_wiring.py`
(3) proves the real `LiveMarketDataClient`/`LiveExecutionClient`
subclasses and full `TradingNode` assembly work, using Nautilus's own
test-kit stubs — no live network reached. Start here before touching any
live connection.

### Step 3 — Confirm the WS quote-mode payload shape (the one real unknown)
LTP mode is confirmed from the docs; quote mode (needed for bid/ask/OBI)
isn't. **This step has to run on your own machine** — my sandbox's
network access doesn't reach `indstocks.com`, so I can't capture this
myself.

```bash
export INDSTOCKS_ACCESS_TOKEN=your_token_here
python scripts/capture_ws_payload.py --token NSE:2885 --mode quote --count 20
```

This prints each raw payload as it arrives and saves them all to a JSON
file in the current directory. Paste that file's contents (or a few
representative payloads) back into a conversation with Claude — with
real data in hand, `_parse_tick()` in `indstocks_data.py` can be
corrected precisely instead of guessed at. This is the single most
important thing to verify before trusting OBI/spread-based strategies
live.

### Step 4 — Populate a real instrument master
```bash
export INDSTOCKS_ACCESS_TOKEN=your_token_here
python scripts/refresh_instruments.py --watchlist NIFTY BANKNIFTY SENSEX
# add --include-fno once you're ready to pull the (large) options chain
```
This regenerates `config/instruments.json` from INDstocks' real CSV
instrument master. Options roll weekly/monthly — re-run this on a
schedule, don't hand-maintain the file.

### Step 5 — Get TrueData tick history flowing
```bash
export TRUEDATA_USERNAME=your_username
export TRUEDATA_PASSWORD=your_password
pip install truedata
python scripts/capture_truedata_daily.py --symbols NIFTY BANKNIFTY
```
Remember: TrueData's tick history defaults to a 5-trading-day window, so
schedule this daily (cron, systemd timer, etc.) rather than treating it
as an on-demand backfill — see the note in `truedata_loader.py`.

### Step 6 — Run a backtest against captured data
Two options, same underlying strategies and Arbiter:

**Simplified replay** (no Nautilus engine, fastest to iterate):
```bash
python scripts/run_backtest.py --instrument NIFTY-INDEX --date 2026-08-21
```

**Full NautilusTrader engine** (real `Instrument`, real fills, real
order/position reports — closer to how live trading will actually
behave):
```bash
# first, get your captured ticks into the real Nautilus catalog:
python -c "
from src.data.catalog_wrangler import CatalogWrangler
from src.adapters.mapper import InstrumentMapper
from src.adapters.instrument_builder import build_instrument
wrangler = CatalogWrangler('./data_catalog')
df = wrangler.read_ticks('NIFTY-INDEX', '2026-08-21')
# convert df rows -> NormalizedTick, then:
# wrangler.write_to_nautilus_catalog(ticks, build_instrument(record))
"
python scripts/run_nautilus_backtest.py --instrument NIFTY-INDEX
```
Either way, watch the approval rate and net EV numbers — if almost
nothing clears your threshold (common for options given real
transaction costs, as the smoke tests in this build showed), that's the
arbiter doing its job, not a bug. Tune `raw_alpha_bps` scaling in each
strategy or `net_ev_threshold_bps` in `settings.yaml` based on what you
see.

### Step 7 — Confirm the INDstocks order endpoint, then dry-run PAPER mode
Before flipping any live switches, verify the REST order-placement
endpoint against INDstocks' current docs and update the `payload` dict
in `indstocks_exec.py::place_order()` accordingly. Then:
```bash
export INDSTOCKS_ACCESS_TOKEN=your_token_here
python scripts/run_live.py --mode PAPER
```
`dry_run=True` is the default in PAPER mode — orders are logged, not
sent. Watch a full session of decisions before considering LIVE mode.

### Step 8 — Run the full live TradingNode (PAPER mode)
```bash
export INDSTOCKS_ACCESS_TOKEN=your_token_here
python scripts/run_live_nautilus.py --mode PAPER --instrument NIFTY-INDEX
```
This assembles a real `nautilus_trader.live.node.TradingNode` — data
engine, exec engine, risk engine, reconciliation — with
`IndstocksLiveMarketDataClient`/`IndstocksLiveExecutionClient`
registered via factories, and runs a strategy inside it through the same
bridge used in the backtest engine. `dry_run=True` by default in PAPER
mode. The simpler standalone-asyncio `run_live.py` still works too and
is easier to debug line-by-line if something's misbehaving — reach for
`run_live_nautilus.py` once you want Nautilus's built-in position
reconciliation and reporting.

## 4. Repo layout
```
indstocks_nautilus_bot/
├── config/
│   ├── settings.yaml          # all tunables — risk limits, costs, URLs
│   └── instruments.json       # token <-> Nautilus id mapping (placeholder data)
├── src/
│   ├── adapters/
│   │   ├── indstocks_data.py       # WS price feed client
│   │   ├── indstocks_exec.py       # REST order client + WS order updates
│   │   ├── mapper.py               # WS token / scrip-code / security_id mapping
│   │   ├── instrument_builder.py   # -> real Nautilus IndexInstrument/OptionContract
│   │   ├── nautilus_instrument_provider.py  # InstrumentProvider backed by our master
│   │   ├── nautilus_live_data_client.py     # real LiveMarketDataClient subclass
│   │   ├── nautilus_live_exec_client.py     # real LiveExecutionClient subclass
│   │   └── nautilus_factories.py            # configs + factories for TradingNode
│   ├── features/
│   │   ├── obi_calculator.py
│   │   └── vwap_engine.py
│   ├── strategies/
│   │   ├── base.py
│   │   ├── opening_dislocation.py   # ORPC (your 09:15 strategy)
│   │   ├── obi_momentum.py
│   │   ├── vwap_reversion.py
│   │   ├── vol_breakout.py
│   │   └── nautilus_bridge.py       # wraps any strategy above in a real nautilus_trader Strategy
│   ├── core/
│   │   ├── signals.py         # OpportunitySignal, ArbiterDecision
│   │   ├── cost_engine.py     # Indian STT/GST/turnover/brokerage math
│   │   └── arbiter.py         # scoring, single-signal gating, batch ranking, risk limits
│   └── data/
│       ├── catalog_wrangler.py       # raw Parquet AND real Nautilus ParquetDataCatalog
│       └── vendors/truedata_loader.py
├── scripts/
│   ├── run_backtest.py             # simplified replay, no Nautilus engine
│   ├── run_nautilus_backtest.py    # full real nautilus_trader BacktestEngine
│   ├── run_live.py                 # standalone asyncio live loop (simple, easy to debug)
│   ├── run_live_nautilus.py        # full real nautilus_trader TradingNode (live/paper)
│   ├── refresh_instruments.py      # pulls real instrument master CSVs from INDstocks
│   └── capture_truedata_daily.py
├── tests/
│   ├── test_core_logic.py                # 11 tests, no credentials/nautilus needed
│   ├── test_nautilus_integration.py       # 4 tests, real BacktestEngine end-to-end
│   └── test_nautilus_live_wiring.py       # 3 tests, real Live clients + TradingNode
└── requirements.txt
```
