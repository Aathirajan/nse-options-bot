"""
scripts/capture_ws_payload.py

Run this ON YOUR OWN MACHINE (not in a sandboxed environment) with a
real INDSTOCKS_ACCESS_TOKEN. It connects to the live price feed,
prints every raw payload it receives, and saves them to a JSON file —
so you can either eyeball the quote-mode field names yourself, or paste
the saved file's contents back into a conversation with Claude to get
_parse_tick() in indstocks_data.py corrected against real data.

This is deliberately separate from indstocks_data.py's own __main__
block — this one captures BOTH ltp and quote mode side by side, saves
everything to disk, and stops automatically after N payloads instead of
running forever, so it's easy to run, share, and re-run.

Usage:
    export INDSTOCKS_ACCESS_TOKEN=your_real_token
    python scripts/capture_ws_payload.py --token NSE:2885 --count 20

    # or subscribe using your own instrument's WS token, e.g. from
    # config/instruments.json after running refresh_instruments.py:
    python scripts/capture_ws_payload.py --token NSE:40000001 --mode quote --count 20

Output: prints each raw payload as it arrives, and writes them all to
    ws_payload_capture_<mode>_<timestamp>.json
in the current directory when done (or on Ctrl+C).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets


async def capture(token: str, mode: str, count: int, ws_url: str, access_token: str) -> list[dict]:
    payloads: list[dict] = []

    print(f"Connecting to {ws_url} ...")
    async with websockets.connect(ws_url, additional_headers={"Authorization": access_token}) as ws:
        subscribe_msg = {"action": "subscribe", "mode": mode, "instruments": [token]}
        await ws.send(json.dumps(subscribe_msg))
        print(f"Sent subscribe: {subscribe_msg}")
        print(f"Waiting for up to {count} payloads (Ctrl+C to stop early)...\n")

        async for raw_message in ws:
            try:
                payload = json.loads(raw_message)
            except json.JSONDecodeError:
                print(f"[non-JSON, likely heartbeat]: {raw_message!r}")
                continue

            print(json.dumps(payload, indent=2))
            print("-" * 40)
            payloads.append(payload)

            if len(payloads) >= count:
                break

    return payloads


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True, help="WS token, e.g. NSE:2885 (RELIANCE, per INDstocks' own docs example)")
    parser.add_argument("--mode", choices=["ltp", "quote"], default="quote")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--ws-url", default="wss://ws-prices.indstocks.com/api/v1/ws/prices")
    args = parser.parse_args()

    access_token = os.environ.get("INDSTOCKS_ACCESS_TOKEN")
    if not access_token:
        raise SystemExit("Set INDSTOCKS_ACCESS_TOKEN before running")

    payloads = []
    try:
        payloads = asyncio.run(capture(args.token, args.mode, args.count, args.ws_url, access_token))
    except KeyboardInterrupt:
        print("\nStopped early by user.")
    finally:
        if payloads:
            out_path = Path(f"ws_payload_capture_{args.mode}_{int(time.time())}.json")
            out_path.write_text(json.dumps(payloads, indent=2))
            print(f"\nSaved {len(payloads)} payloads to {out_path}")
            print(
                "Paste this file's contents (or a few representative payloads) "
                "back to Claude to get _parse_tick() in src/adapters/indstocks_data.py "
                "corrected against real data."
            )
        else:
            print("\nNo payloads captured.")


if __name__ == "__main__":
    main()
