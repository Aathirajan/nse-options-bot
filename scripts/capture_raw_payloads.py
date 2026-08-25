"""Capture raw INDstocks quote messages and instrument-master responses.

This script deliberately preserves WebSocket messages exactly as received.
It does not reuse the application's parser or normalize payload fields.
Run during market hours with candidate WS tokens confirmed manually:

    python scripts/capture_raw_payloads.py \
        --index-token NIDX:26000 \
        --index-token BIDX:YOUR_BANKNIFTY_TOKEN \
        --index-token BIDX:YOUR_SENSEX_TOKEN \
        --option-token NFO:... \
        --option-token NFO:... \
        --duration 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import websockets

REST_BASE_URL = "https://api.indstocks.com"
WS_URL = "wss://ws-prices.indstocks.com/api/v1/ws/prices"


def _write_rest_dump(access_token: str, output_path: Path) -> None:
    with httpx.Client(
        base_url=REST_BASE_URL,
        headers={"Authorization": access_token},
        timeout=30.0,
    ) as client, output_path.open("w", encoding="utf-8") as output:
        for source in ("index", "fno"):
            response = client.get("/market/instruments", params={"source": source})
            response.raise_for_status()
            output.write(f"=== source={source} status={response.status_code} ===\n")
            output.write(response.text)
            if not response.text.endswith("\n"):
                output.write("\n")


def _summarize_payloads(payloads: list[dict]) -> None:
    by_mode: dict[str, list[dict]] = {}
    for payload in payloads:
        mode = str(payload.get("mode", "unknown"))
        by_mode.setdefault(mode, []).append(payload)

    print("\nPayload summary:")
    for mode, messages in sorted(by_mode.items()):
        fields = sorted({key for message in messages for key in message})
        example = json.dumps(messages[0], indent=2)
        print(f"- mode={mode}: {len(messages)} messages; envelope fields={fields}")
        print(f"  first example={example}")
        data_fields = sorted(
            {
                key
                for message in messages
                for key in (message.get("data") or {})
                if isinstance(message.get("data"), dict)
            }
        )
        print(f"  data fields={data_fields}")


async def _capture(
    tokens: list[str], duration_seconds: int, output_path: Path, access_token: str
) -> list[dict]:
    payloads: list[dict] = []
    subscribe_message = {"action": "subscribe", "mode": "quote", "instruments": tokens}
    deadline = time.monotonic() + duration_seconds

    async with websockets.connect(WS_URL, additional_headers={"Authorization": access_token}) as ws:
        await ws.send(json.dumps(subscribe_message))
        print(f"Subscribed to {len(tokens)} tokens; capturing for {duration_seconds}s")
        with output_path.open("w", encoding="utf-8") as output:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    raw_message = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if isinstance(raw_message, bytes):
                    raw_message = raw_message.decode("utf-8")
                output.write(raw_message)
                output.write("\n")
                output.flush()
                try:
                    payload = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                if isinstance(payload, dict):
                    payloads.append(payload)

    return payloads


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index-token", action="append", required=True)
    parser.add_argument("--option-token", action="append", required=True)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--quote-output", default="logs/raw_quote_capture.jsonl")
    parser.add_argument("--instruments-output", default="logs/raw_instruments_dump.txt")
    args = parser.parse_args()

    access_token = os.environ.get("INDSTOCKS_ACCESS_TOKEN")
    if not access_token:
        raise SystemExit("Set INDSTOCKS_ACCESS_TOKEN before running")
    if len(args.index_token) < 3:
        raise SystemExit("Provide candidate WS tokens for NIFTY, BANKNIFTY, and SENSEX")
    if len(args.option_token) < 2:
        raise SystemExit("Provide at least two option WS tokens")

    quote_path = Path(args.quote_output)
    instruments_path = Path(args.instruments_output)
    quote_path.parent.mkdir(parents=True, exist_ok=True)
    instruments_path.parent.mkdir(parents=True, exist_ok=True)

    _write_rest_dump(access_token, instruments_path)
    tokens = args.index_token + args.option_token
    payloads = asyncio.run(_capture(tokens, args.duration, quote_path, access_token))
    _summarize_payloads(payloads)
    print(f"\nSaved raw quote messages to {quote_path}")
    print(f"Saved raw instrument responses to {instruments_path}")
    print(f"Captured {len(payloads)} JSON messages")


if __name__ == "__main__":
    main()
