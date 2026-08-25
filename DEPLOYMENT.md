# Deployment Guide — NautilusTrader + INDstocks Opportunity Engine

## Overview

This document covers how to deploy the trading engine in three environments:
1. **Local Development** — for testing and strategy tuning
2. **Paper Trading** — with real INDstocks connection, simulated orders
3. **Live Trading** — real orders against real INDstocks account

---

## Prerequisites

### System Requirements
- **OS:** Windows, macOS, or Linux
- **Python:** 3.11+ (tested with 3.13.1)
- **Network:** Stable internet connection to INDstocks and TrueData APIs
- **INDstocks Account:** Access token (paper or live)
- **TrueData Account:** (Optional) credentials for historical tick data

### Dependencies
```bash
pip install -r requirements.txt
```

This installs:
- `nautilus_trader==1.231.0` — core backtesting/live trading engine
- `websockets>=12.0` — WebSocket price/order feeds
- `httpx>=0.27.0` — REST API client
- `pandas>=2.2.0` — data processing
- `pydantic>=2.7.0` — configuration validation
- `pyyaml>=6.0.1` — config files
- `pytest>=9.0.0` — unit tests
- And others (see requirements.txt)

---

## Environment Setup

### 1. Configure Credentials

Create `config/settings.local.yaml` (add to `.gitignore`, never commit):

```yaml
# Copy from config/settings.yaml and fill in real values:

mode: "PAPER"  # or "LIVE" — switch only after validating PAPER mode

indstocks:
  access_token: "your-real-indstocks-token-here"
  rest_base_url: "https://api.indstocks.com"
  ws_prices_url: "wss://ws-prices.indstocks.com/api/v1/ws/prices"
  ws_orders_url: "wss://ws-order-updates.indstocks.com/api/v1/ws/trades"

truedata:
  username: "your-truedata-username"
  password: "your-truedata-password"
```

Alternatively, set environment variables:
```bash
export INDSTOCKS_ACCESS_TOKEN="your-token-here"
export TRUEDATA_USERNAME="username"
export TRUEDATA_PASSWORD="password"
```

### 2. Download Instrument Master

The engine requires an up-to-date instrument mapping (NIFTY, SENSEX, and options contracts):

```bash
export INDSTOCKS_ACCESS_TOKEN="your-token-here"
python scripts/refresh_instruments.py --watchlist NIFTY SENSEX --include-fno
```

This regenerates `config/instruments.json` from the real INDstocks API.

**Note:** SENSEX WS token is not yet confirmed. If it fails, capture it first:

```bash
python scripts/capture_ws_payload.py
# Subscribe to SENSEX in the prompt, copy the real token, then add to:
# scripts/refresh_instruments.py → ws_overrides dict
```

### 3. Verify Setup

Run the test suite to ensure everything is configured:

```bash
python -m pytest tests/ -v
```

Should show: **33 passed** ✅

---

## Deployment Scenarios

---

## Scenario 1: Local Backtest

Test a strategy against historical data **without connecting to live markets.**

### Quick Start

```bash
python scripts/run_backtest.py
```

This:
- Loads NIFTY ticks from `data_catalog/` (if available)
- Runs all 4 strategies (OBI, VWAP, Vol Breakout, ORPC) through the Nautilus backtest engine
- Reports trades, fills, P&L, and strategy performance

### Configuration

Edit `config/settings.yaml` strategy section:
```yaml
strategies:
  obi_momentum:
    enabled: true
    quantity: 1
  vwap_reversion:
    enabled: true
    quantity: 1
  vol_breakout:
    enabled: true
    quantity: 1
  orpc:
    enabled: true
    quantity: 1
```

### Expected Output

```
[2026-08-24 14:32:10] INFO - OBI momentum: APPROVED BUY qty=1 net_ev=45.2bps
[2026-08-24 14:32:15] INFO - Order filled: 23450.50 @ 65 lots
[2026-08-24 14:33:02] INFO - Close: +1250 INR (0.42% on NIFTY-INDEX)
```

---

## Scenario 2: Paper Trading (Simulated Orders)

Connect to real INDstocks feeds but **do NOT submit real orders** — good for:
- Validating signal quality
- Testing order handling logic
- Checking cost calculations
- Debugging connectivity issues

### Start Paper Mode

```bash
export INDSTOCKS_ACCESS_TOKEN="your-token"
python scripts/run_live_nautilus.py --mode PAPER --instrument NIFTY-INDEX
```

Or the simpler asyncio loop (easier to debug):

```bash
python scripts/run_live.py --mode PAPER --config config/settings.yaml
```

**What happens:**
1. Connects to INDstocks WS price feed for NIFTY, SENSEX
2. Resolves today's ATM CE/PE for ORPC strategy
3. Streams live ticks into all 4 strategies
4. Runs signals through the Arbiter (cost calc, risk limits)
5. **Prints decisions instead of placing orders**

### Monitoring

Watch the logs:
```bash
tail -f logs/engine.log
```

Look for:
- Signal emissions: `STRATEGY emitted BUY/SELL`
- Arbiter decisions: `APPROVED` (good EV) or `REJECTED` (too low EV)
- Order submissions: `Submitting order (dry_run=True)` — no actual order placed

### Stop

Press `Ctrl+C` to exit gracefully.

---

## Scenario 3: Live Trading

**Real orders against your INDstocks account.** Start here only after:
- ✅ Local backtest runs successfully
- ✅ Paper mode runs for ≥30 minutes with signal quality you trust
- ✅ Cost assumptions match your actual broker fees
- ✅ Risk limits are set appropriately

### Pre-Flight Checklist

- [ ] `config/settings.local.yaml` has real credentials
- [ ] `mode: "LIVE"` in settings (not "PAPER")
- [ ] `config/instruments.json` is up-to-date (run `refresh_instruments.py` within last 24h)
- [ ] Test suite passes: `pytest tests/ -v`
- [ ] Watched PAPER mode for ≥30 minutes, signal quality looks good
- [ ] Risk limits reviewed: `max_daily_loss_inr`, `max_concurrent_positions`, etc.

### Start Live Mode

```bash
export INDSTOCKS_ACCESS_TOKEN="your-live-token"
python scripts/run_live_nautilus.py --mode LIVE --instrument NIFTY-INDEX
```

**WARNING:** Orders will be placed immediately upon approval. Monitor closely.

### During Live Run

1. **Monitor positions**
   ```bash
   tail -f logs/engine.log | grep "Order"
   ```

2. **Check INDstocks broker dashboard**
   - Confirm orders are showing up
   - Verify fills match engine's expected fills

3. **Kill switch**
   - Press `Ctrl+C` to stop the engine
   - Already-placed orders will NOT be automatically cancelled
   - Log in to INDstocks to cancel manually if needed

4. **Reconciliation**
   - Engine reconciles broker position vs. local state every 300s (configurable in settings.yaml)
   - If mismatch detected, engine halts with error
   - Manually review broker, then restart

### Stop and Review

```bash
# End of day summary
grep "APPROVED\|REJECTED" logs/engine.log | tail -20
```

---

## Operational Procedures

### Capturing Real SENSEX WS Token

SENSEX's WebSocket token (`BIDX:*`) is not derivable from REST API. Capture once:

```bash
python scripts/capture_ws_payload.py
```

Follow prompts:
1. Subscribe to SENSEX in the live feed
2. Watch for the `instrument` field in incoming messages
3. Note the `BIDX:xxxxx` token
4. Add to `scripts/refresh_instruments.py` ws_overrides, then regenerate:
   ```bash
   python scripts/refresh_instruments.py --watchlist NIFTY SENSEX
   ```

### Daily Instrument Refresh

Run every market open to get latest option contracts:

```bash
python scripts/refresh_instruments.py --watchlist NIFTY SENSEX --include-fno

This ensures ORPC can resolve today's ATM CE/PE correctly.

### Pulling Historical Ticks (TrueData)

If you have a TrueData account, populate historical data:

```bash
python scripts/capture_truedata_daily.py --symbols NIFTY
```

Writes ticks to `data_catalog/raw_ticks/NIFTY-INDEX/YYYY-MM-DD.parquet`.

---

## Troubleshooting

### Test Suite Fails

```bash
python -m pytest tests/ -v
```

**Common issues:**
- Missing dependencies: `pip install -r requirements.txt`
- Python version: `python --version` (should be 3.11+)
- SENSEX token missing: See "Capturing Real SENSEX WS Token" above

### Connection Errors to INDstocks

**Error:** `ConnectionRefusedError` or `WS timeout`

**Fixes:**
- Check internet connection: `ping api.indstocks.com`
- Verify token is valid: `curl -H "Authorization: token" https://api.indstocks.com/market/instruments?source=index`
- INDstocks API might be down (check their status page)

### Signals Not Emitting

**Symptom:** Paper mode runs but no signals appear in log

**Check:**
1. Instrument data is real: `grep "NIFTY-INDEX" config/instruments.json` (should exist)
2. Strategy is enabled in settings: `obi_momentum.enabled: true`
3. Price volatility: OBI, VWAP, Vol strategies need actual price movement to trigger
4. Log level: `logging.level: "DEBUG"` in settings.yaml for more verbosity

### Orders Not Filling


**Symptom:** Order placed but no fill for 10+ seconds

**Possible causes:**
- Order placed at bad price (no liquidity)
- INDstocks order queue backed up
- WS order-update feed delayed (check feed subscription)

**Verify:**
```bash
# Check order book in INDstocks broker UI
# vs. engine's recorded pending orders in logs
grep "Pending" logs/engine.log
```

---

## Monitoring & Alerting

### Recommended Setup

1. **Log aggregation:** Forward `logs/engine.log` to Splunk, DataDog, or similar
2. **Order monitoring:** Watch for `APPROVED` signal ratio (good: 20-30%, bad: >50%)
3. **P&L tracking:** Summarize daily realized P&L from `logs/engine.log`
4. **Alert on:** Position limit breach, daily loss exceeded, feed disconnection

### Key Metrics

Track in your monitoring tool:
- **Signal rate:** signals/minute (baseline → detect hung strategies)
- **Approval rate:** approved / emitted signals (sanity check on cost model)
- **Fill rate:** fills / submitted orders (detect broker connectivity issues)
- **Latency:** signal emit → order fill (should be <5s, >10s means lag)

---

## Security & Best Practices

### Credential Management

- **Never commit** credentials to Git (`.gitignore` includes `settings.local.yaml`)
- **Use environment variables** for automated deployments
- **Rotate tokens** regularly in INDstocks account settings
- **Log access:** Keep audit trail of who accessed live credentials

### Risk Management

- **Start small:** First live day, set `max_position_size_units: 10` (not 150)
- **Monitor costs:** Real STT/GST can differ from config — reconcile daily
- **Review risk limits:** `max_daily_loss_inr`, `max_concurrent_positions` should reflect your risk appetite
- **Halt conditions:** If daily loss > limit, engine stops automatically

### Operational

- **Avoid peak hours:** Start live trading outside market open (9:15 IST) first day
- **Gradual ramp:** Day 1: low quantity, Day 2-3: validate fills, Day 4+: normal quantity
- **Manual review:** Review broker dashboard every hour first week
- **Kill switch training:** Ensure you can quickly stop the engine via `Ctrl+C` or process kill

---

## Deployment Checklist

- [ ] Python 3.11+ installed
- [ ] `pip install -r requirements.txt` successful
- [ ] `pytest tests/ -v` → 33 passed
- [ ] `config/settings.local.yaml` created with real credentials
- [ ] `config/instruments.json` up-to-date (`refresh_instruments.py` run)
- [ ] Paper mode ran for ≥30 minutes with acceptable signal quality
- [ ] Risk limits reviewed and understood
- [ ] Logging configured (check `logs/engine.log` path is writable)
- [ ] Monitoring/alerting set up (at minimum: tail logs in separate terminal)
- [ ] Kill switch procedure tested (can stop engine via Ctrl+C)
- [ ] INDstocks account reconciliation verified (positions match between engine and broker)
- [ ] Ready to go live!

---

## Support & Debugging

### Logs

All activity is logged to `logs/engine.log`. Key sections:
- **Tick stream:** `RawTick received`
- **Signals:** `STRATEGY emitted`
- **Decisions:** `Arbiter evaluation`
- **Orders:** `Order placed`, `Order filled`
- **Errors:** `ERROR` (always search here first)

### Common Commands

```bash
# Follow live log
tail -f logs/engine.log

# Count approved signals today
grep "APPROVED" logs/engine.log | wc -l

# Get P&L summary
grep "realized\|P&L" logs/engine.log

# Find errors only
grep "ERROR\|Exception" logs/engine.log

# Instrument master status
python -c "from src.adapters.mapper import InstrumentMapper; m = InstrumentMapper('config/instruments.json'); print(f'Instruments loaded: {len(m._by_nautilus_id)}')"
```

### Emergency Stop

```bash
# Kill the engine process
pkill -f "run_live_nautilus.py"

# Then manually cancel any open orders in INDstocks broker UI
```

---

## Next Steps

1. **Local setup:** Follow "Environment Setup" above
2. **Test:** Run `pytest tests/ -v` ✓
3. **Backtest:** Try `python scripts/run_backtest.py`
4. **Paper trade:** Run PAPER mode for a session
5. **Go live:** When confident, switch to LIVE mode

Good luck! 🚀

---

**Last Updated:** 2026-08-24  
**Version:** 1.0  
**Status:** Production-ready for NIFTY + SENSEX
