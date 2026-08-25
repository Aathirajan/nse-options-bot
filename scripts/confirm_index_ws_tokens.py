"""Manually verify an INDstocks index WS token against live quote output.

Usage (PowerShell):
    $env:INDSTOCKS_ACCESS_TOKEN = '<token>'
    python scripts/confirm_index_ws_tokens.py --symbol NIFTY --candidate-token NIDX:...

The script only prints raw messages. Confirm the instrument identity from the
returned values before writing the token to config/instruments.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import websockets

WS_URL = "wss://ws-prices.indstocks.com/api/v1/ws/prices"


async def confirm(symbol: str, candidate_token: str, duration: int) -> None:
    access_token = os.environ.get("INDSTOCKS_ACCESS_TOKEN")
    if not access_token:
        raise SystemExit("Set INDSTOCKS_ACCESS_TOKEN before running")

    async with websockets.connect(
        WS_URL, additional_headers={"Authorization": access_token}
    ) as ws:
        await ws.send(json.dumps({
            "action": "subscribe",
            "mode": "quote",
            "instruments": [candidate_token],
        }))
        print(f"Subscribed candidate {candidate_token} for {symbol}; waiting {duration}s")
        try:
            while True:
                raw_message = await asyncio.wait_for(ws.recv(), timeout=duration)
                print(raw_message)
        except asyncio.TimeoutError:
            print("No further messages before timeout")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True, choices=["NIFTY", "SENSEX"])
    parser.add_argument("--candidate-token", required=True, help="Candidate NIDX:/BIDX: token")
    parser.add_argument("--duration", type=int, default=30)
    args = parser.parse_args()
    asyncio.run(confirm(args.symbol, args.candidate_token, args.duration))


if __name__ == "__main__":
    main()
