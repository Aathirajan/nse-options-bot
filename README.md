# NSE Options Bot

Multi-strategy trading engine for Indian index options (NIFTY, SENSEX) built on NautilusTrader. Several strategies emit trade signals, and an arbiter scores each one against real Indian trading costs (STT, GST, exchange fees, brokerage), approving only trades with positive expected value. Supports backtesting and paper/live trading via the INDstocks API.

## How to run

Prerequisites: Python 3.11+, an INDstocks API token.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set INDSTOCKS_ACCESS_TOKEN (TrueData login optional)

python scripts/refresh_instruments.py --watchlist NIFTY SENSEX --include-fno
python scripts/run_backtest.py
python scripts/run_live.py --mode PAPER --config config/settings.yaml
python -m pytest tests/ -v
```

License: MIT
