# NautilusTrader + INDstocks Trading Engine Audit Report
**Date:** 2026-08-24  
**Python Version:** 3.13.1  
**Test Suite:** 33/33 tests PASSED ✓

---

## Summary
The codebase is well-structured with strong test coverage and good documentation. However, there are **2 critical blocking issues** related to instrument configuration that will cause runtime failures, plus several medium-priority maintenance items. See below for prioritized findings.

---

## CRITICAL ISSUES (BLOCKS OPERATION)

### 1. Index Instruments Missing WS Token Overrides
**Severity:** CRITICAL — Will raise ValueError at runtime  
**Files Affected:**
- [config/instruments.json](config/instruments.json#L10) — SENSEX has `ws_token_override: null`
- [config/instruments.json](config/instruments.json#L32) — BANKNIFTY has `ws_token_override: null`
- [scripts/refresh_instruments.py](scripts/refresh_instruments.py#L280) — Only NIFTY token is hardcoded in `ws_overrides`

**Root Cause:**  
Per [src/adapters/mapper.py](src/adapters/mapper.py#L14-L43)'s own docstring (confirmed against INDstocks docs), **INDEX instruments' WS tokens are NOT derivable from their REST security_id**. NIFTY uses `NIDX:26000`, but BANKNIFTY (`BIDX:*`) and SENSEX (`BIDX:*`) tokens are unknown to the codebase.

When any index record without `ws_token_override` calls `.ws_token` property (e.g., in [src/adapters/mapper.py#L60-L68](src/adapters/mapper.py#L60-L68)), it raises:
```
ValueError: '{nautilus_id}' is an INDEX instrument with no ws_token_override set
— the WS token cannot be derived from its REST security_id...
```

**Code Path to Failure:**
1. [scripts/run_live.py#L145](scripts/run_live.py#L145) — `build_watchlist_ws_tokens()` calls `mapper.to_ws_token()`
2. [src/adapters/mapper.py#L107](src/adapters/mapper.py#L107) — `to_ws_token()` calls `get_record().ws_token`
3. [src/adapters/mapper.py#L62-L68](src/adapters/mapper.py#L62-L68) — `.ws_token` property raises if INDEX has no override

**Where it Actually Fails in Tests:**
Test [tests/test_live_subscriptions.py::test_nautilus_watchlist_fails_without_confirmed_index_token](tests/test_live_subscriptions.py#L38) **intentionally verifies this failure** — so the test passes, but live operation will fail.

**Fix Required:**
1. Capture real BANKNIFTY and SENSEX WS tokens via [scripts/capture_ws_payload.py](scripts/capture_ws_payload.py) (subscribe in real WS feed, see what tokens arrive)
2. Add tokens to `ws_overrides` dict in [scripts/refresh_instruments.py#L280](scripts/refresh_instruments.py#L280)
3. Re-run `python scripts/refresh_instruments.py --watchlist NIFTY BANKNIFTY SENSEX` to regenerate [config/instruments.json](config/instruments.json)
4. Verify all three indices have non-null `ws_token_override` before starting live trading

---

### 2. Missing Test Dependency: pytest
**Severity:** CRITICAL — Tests won't run on clean install  
**Files Affected:**
- [requirements.txt](requirements.txt) — Does NOT list pytest
- [tests/test_indstocks_data.py#L7](tests/test_indstocks_data.py#L7) — `import pytest`
- [tests/test_live_subscriptions.py#L5](tests/test_live_subscriptions.py#L5) — `import pytest`

**Test Suite Status:**  
All 33 tests passed in current environment because pytest is installed separately, but `pip install -r requirements.txt` will not install pytest, causing failures on fresh setup.

**Fix Required:**  
Add to [requirements.txt](requirements.txt):
```
pytest>=9.0.0
pytest-asyncio>=1.3.0
```

---

## HIGH-PRIORITY ISSUES (MAINTENANCE/CODE DEBT)

### 3. Outdated TODO Comment vs. Actual Implementation
**Severity:** HIGH — Misleading developer  
**File:** [src/adapters/indstocks_data.py#L260-L277](src/adapters/indstocks_data.py#L260-L277)

**Issue:**  
The TODO block describes building a Nautilus `LiveMarketDataClient` wrapper as if it's not done:
```python
# TODO: Nautilus DataClient wiring
#
# To plug this into NautilusTrader as a proper LiveMarketDataClient, wrap
# IndstocksPriceFeed inside a subclass of nautilus_trader's live data
# client base class...
```

**Reality:**  
This is **already implemented** in [src/adapters/nautilus_live_data_client.py](src/adapters/nautilus_live_data_client.py) and **fully tested** in [tests/test_indstocks_data.py::test_fixture_quote_builds_nautilus_quote_ticks](tests/test_indstocks_data.py#L46).

**Fix Required:**  
Remove the TODO block from [src/adapters/indstocks_data.py#L260-L277](src/adapters/indstocks_data.py#L260-L277) or replace with a note directing to the actual implementation.

---

### 4. Version-Specific Constructor Risk (Not Confirmed)
**Severity:** HIGH — Maintenance trap  
**File:** [src/data/catalog_wrangler.py#L10-L16](src/data/catalog_wrangler.py#L10-L16)

**Issue:**  
The `write_to_nautilus_catalog()` method at [src/data/catalog_wrangler.py#L103-L140](src/data/catalog_wrangler.py#L103-L140) has a TODO note about confirming `QuoteTick` constructor signatures:
```python
# Confirmed against nautilus_trader==1.231.0's actual constructor
# signatures (inspected directly)...
```

This is pinned to version 1.231.0. Any upgrade to `nautilus_trader` could break this without warning if constructor signatures change.

**Status:** Not currently blocking (code was inspected at 1.231.0 and is correct), but is a known maintenance risk.

**Recommended:** Add version constraint to requirements.txt:
```
nautilus_trader==1.231.0  # DON'T upgrade without re-confirming QuoteTick constructors
```

---

### 5. TrueData Symbol Mapping Stubbed Out
**Severity:** MEDIUM — Feature incomplete  
**File:** [src/data/vendors/truedata_loader.py#L33-L40](src/data/vendors/truedata_loader.py#L33-L40)

**Issue:**  
`TRUEDATA_SYMBOL_MAP` is hardcoded with only indices:
```python
TRUEDATA_SYMBOL_MAP: dict[str, str] = {
    "NIFTY": "NIFTY-INDEX",
    "BANKNIFTY": "BANKNIFTY-INDEX",
    # options and SENSEX need their own literal symbol strings...
}
```

The comment explicitly states options and SENSEX are incomplete. This module will raise `KeyError` if you try to fetch TrueData ticks for any unmapped symbol.

**Status:** Documented limitation, not hidden. Used only in [scripts/capture_truedata_daily.py](scripts/capture_truedata_daily.py) (a utility script, not live trading).

**Recommended:** Keep as-is (it's safe) until options history capture is needed. Then generate the full mapping from TrueData's symbol master and replace the hardcoded dict.

---

## MEDIUM PRIORITY (CODE QUALITY)

### 6. No .gitignore File Found
**Severity:** MEDIUM — Potential secret leak  
**Files Affected:**
- No `.gitignore` file exists in workspace root

**Risk:**  
If this repo is version-controlled, secrets could be accidentally committed:
- `config/settings.local.yaml` (which [config/settings.yaml](config/settings.yaml#L7) instructs to create and populate with real credentials)
- `.env` files with `INDSTOCKS_ACCESS_TOKEN`
- `.venv/` environment cache

**Status:** [config/settings.yaml](config/settings.yaml#L7) already warns: "NEVER commit settings.local.yaml — add it to .gitignore", so developer intent is clear. But no actual `.gitignore` enforces this.

**Fix Required:**  
Create `.gitignore`:
```
settings.local.yaml
.env
.env.local
.venv/
__pycache__/
*.pyc
.pytest_cache/
.qodo/
*.egg-info/
```

---

### 7. Ambiguous Order Status Vocabulary in Live Execution
**Severity:** LOW — Documented, handled gracefully  
**File:** [src/adapters/indstocks_exec.py#L30-L37](src/adapters/indstocks_exec.py#L30-L37)

**Issue:**  
INDstocks' own API docs have an inconsistency:
- **WebSocket order-update feed** example uses: `"PARTIALLY_EXECUTED"` (underscore)
- **REST order-book endpoint** uses: `"PARTIALLY FILLED"` (space, no underscore)

Neither matches the other. The docs themselves contain this exact inconsistency.

**Current Handling:**  
Code at [src/adapters/indstocks_exec.py#L87-L100](src/adapters/indstocks_exec.py#L87-L100) treats status as an opaque string in `OrderUpdate`, avoiding the enum trap.

**Status:** Problem is acknowledged in docstring, handled defensively. No action needed, but notable as a vendor API quirk.

---

## PASSING AUDIT AREAS

### ✓ Test Suite Health
- **Result:** 33/33 tests PASSED
- **Coverage:** Unit tests for arbiter, cost engine, all 4 strategies, mapper roundtrips, fixture quote parsing, live wiring
- **Note:** One minor warning about deprecated `Timestamp.utcnow()` in [tests/test_nautilus_integration.py#L137](tests/test_nautilus_integration.py#L137) (Pandas 4 deprecation, non-blocking)

### ✓ Dependency Hygiene
- [requirements.txt](requirements.txt) lists all major imports used: `nautilus_trader`, `websockets`, `httpx`, `pandas`, `pyarrow`, `pydantic`, `pyyaml`, `python-dateutil`, `truedata`, `tenacity`
- **Gap:** `pytest` and `pytest-asyncio` missing (see CRITICAL issue #2)

### ✓ Secrets & Credentials
- [config/settings.yaml](config/settings.yaml) **does NOT hardcode** any tokens, passwords, or API keys
- All credentials sourced from environment variables: `${INDSTOCKS_ACCESS_TOKEN}`, `${TRUEDATA_USERNAME}`, `${TRUEDATA_PASSWORD}`
- Instructions explicitly warn against committing real credentials

### ✓ Code-to-Docstring Alignment
- Docstrings in [src/adapters/mapper.py](src/adapters/mapper.py), [src/adapters/indstocks_data.py](src/adapters/indstocks_data.py), [src/adapters/indstocks_exec.py](src/adapters/indstocks_exec.py) are **detailed and accurate**
- Implementation matches claims (tested via test suite and integration tests)
- Only exception: outdated TODO in [indstocks_data.py#L260](src/adapters/indstocks_data.py#L260) (already implemented elsewhere)

### ✓ Live Wiring & Nautilus Integration
- [scripts/run_live_nautilus.py](scripts/run_live_nautilus.py) correctly assembles full `TradingNodeConfig` with data/exec/risk engines
- [src/adapters/nautilus_live_data_client.py](src/adapters/nautilus_live_data_client.py) and [src/adapters/nautilus_live_exec_client.py](src/adapters/nautilus_live_exec_client.py) implement correct async signatures for nautilus_trader 1.231.0
- Confirmed via passing integration test [tests/test_nautilus_integration.py::test_full_backtest_engine_runs_and_can_submit_orders](tests/test_nautilus_integration.py#L125)

### ✓ Instrument Mapping (Except Index Tokens)
- `InstrumentMapper` correctly handles the three INDstocks identifier formats: WS token, REST scrip code, REST security_id
- Fallback for INDEX instruments (non-derivable WS tokens) is **properly defensive**: raises explicitly rather than silently guessing
- F&O contract mapping works correctly (tokens ARE derivable for options)

### ✓ Cost Engine & Arbiter Logic
- [src/core/cost_engine.py](src/core/cost_engine.py) correctly calculates STT, brokerage, GST, SEBI charges per Indian market rules
- [src/core/arbiter.py](src/core/arbiter.py) enforces risk limits, rejects low-EV signals, ranks batch signals
- Tested against multiple scenarios in [tests/test_core_logic.py](tests/test_core_logic.py) — all pass

### ✓ Strategy Implementations
All four strategy implementations pass unit tests:
- [src/strategies/obi_momentum.py](src/strategies/obi_momentum.py) — OBI (order book imbalance) detection
- [src/strategies/vwap_reversion.py](src/strategies/vwap_reversion.py) — VWAP stretch detection
- [src/strategies/vol_breakout.py](src/strategies/vol_breakout.py) — Volatility breakout on low volume
- [src/strategies/opening_dislocation.py](src/strategies/opening_dislocation.py) — ORPC (opening range premium convergence)

---

## DETAILED FINDINGS TABLE

| Priority | Category | Issue | File(s) | Impact | Status |
|----------|----------|-------|---------|--------|--------|
| **CRITICAL** | Config | Missing WS token overrides for BANKNIFTY, SENSEX | `config/instruments.json`, `scripts/refresh_instruments.py` | Runtime ValueError when subscribing to indices | Unfixed — blocks live trading on these symbols |
| **CRITICAL** | Dependencies | pytest not in requirements.txt | `requirements.txt` | Fresh install fails to run tests | Unfixed — add pytest, pytest-asyncio |
| **HIGH** | Documentation | Outdated TODO re: Nautilus client wiring | `src/adapters/indstocks_data.py` | Developer confusion (implementation exists elsewhere) | Unfixed — remove/update TODO |
| **HIGH** | Maintenance | Version-specific constructor signatures | `src/data/catalog_wrangler.py` | Silent breakage on nautilus_trader upgrade | Unfixed — pin version or document risk |
| **MEDIUM** | Feature | TrueData symbol mapping incomplete | `src/data/vendors/truedata_loader.py` | Cannot fetch options/SENSEX from TrueData | As-designed — documented limitation |
| **MEDIUM** | DevOps | No .gitignore file | (workspace root) | Secrets could leak if repo committed | Unfixed — create .gitignore |
| **LOW** | API Quirk | INDstocks WS/REST status vocabulary mismatch | `src/adapters/indstocks_exec.py` | None (handled defensively) | Documented, no action needed |

---

## RECOMMENDED NEXT STEPS (Prioritized)

### Immediate (Before Live Trading)
1. **Capture BANKNIFTY and SENSEX WS tokens** using [scripts/capture_ws_payload.py](scripts/capture_ws_payload.py)
2. **Update ws_overrides** in [scripts/refresh_instruments.py](scripts/refresh_instruments.py#L280)
3. **Regenerate instruments.json** with real tokens
4. **Add pytest to requirements.txt**
5. **Verify test suite runs clean** on fresh install

### Short-term (Before Commit)
6. Create `.gitignore` with sensitive file patterns
7. Remove outdated TODO from [src/adapters/indstocks_data.py#L260](src/adapters/indstocks_data.py#L260)
8. Add version pin to [requirements.txt](requirements.txt) for `nautilus_trader==1.231.0` with comment

### Long-term (Optional)
9. Generate full `TRUEDATA_SYMBOL_MAP` if/when options history capture needed
10. Add E2E test that connects to real INDstocks feed and verifies token subscription succeeds

---

## Conclusion

The codebase is **well-engineered with strong fundamentals**: comprehensive tests, clear separation of concerns, defensive error handling, and good documentation. The two critical issues are **configuration-related, not code-related** — once the WS tokens are captured and pytest is added, the system is ready for live trading. No architectural issues or hidden bugs detected.

