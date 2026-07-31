#!/usr/bin/env python3
"""Refresh the public, read-only ASTEROID market snapshot."""
from __future__ import annotations

import json
import ssl
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

D = Decimal
POOL = "0x76a411f14a704099ba476ce8dffc288a53295218"
GET_RESERVES = "0x0902f1ac"
RPC_URLS = {
    "publicnode": "https://ethereum-rpc.publicnode.com",
    "llamarpc": "https://eth.llamarpc.com",
    "cloudflare": "https://cloudflare-eth.com",
    "1rpc": "https://1rpc.io/eth",
}
DEX_API = f"https://api.dexscreener.com/latest/dex/pairs/ethereum/{POOL}"
OUT = Path(__file__).resolve().parents[1] / "docs" / "data"
try:
    import certifi
    SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL = ssl.create_default_context()


def request_json(url: str, payload: dict | None = None, timeout: int = 15) -> dict:
    body = json.dumps(payload).encode() if payload else None
    headers = {"Accept": "application/json", "User-Agent": "AsteroidResearch/1.0"}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, body, headers)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL) as response:
        return json.load(response)


def rpc(url: str, method: str, params: list):
    result = request_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    return result.get("result")


def words(raw: str) -> list[int]:
    text = raw.removeprefix("0x")
    return [int(text[i:i + 64], 16) for i in range(0, len(text), 64)]


def main() -> None:
    observations, errors = [], []
    for name, url in RPC_URLS.items():
        started = time.time()
        try:
            block_hex = rpc(url, "eth_blockNumber", [])
            block = int(block_hex, 16)
            reserve_raw = rpc(url, "eth_call", [{"to": POOL, "data": GET_RESERVES}, block_hex])
            reserve_words = words(reserve_raw)
            gas_wei = int(rpc(url, "eth_gasPrice", []), 16)
            header = rpc(url, "eth_getBlockByNumber", [block_hex, False])
            observations.append({
                "provider": name,
                "block": block,
                "block_hash": header["hash"],
                "timestamp": int(header["timestamp"], 16),
                "weth_reserve": str(D(reserve_words[0]) / D(10**18)),
                "token_reserve": str(D(reserve_words[1]) / D(10**9)),
                "gas_gwei": str(D(gas_wei) / D(10**9)),
                "latency_ms": round((time.time() - started) * 1000),
            })
        except Exception as exc:
            errors.append({"provider": name, "error": type(exc).__name__})

    dex = (request_json(DEX_API).get("pairs") or [{}])[0]
    previous = {}
    previous_file = OUT / "live.json"
    if previous_file.exists():
        previous = json.loads(previous_file.read_text())
    if not observations:
        observations = previous.get("observations") or []
        if not observations:
            raise RuntimeError("no RPC observation and no previous verified snapshot")
        errors.append({"provider": "all", "error": "using last verified reserve snapshot"})
    blocks = [item["block"] for item in observations]
    tokens = [D(item["token_reserve"]) for item in observations]
    weths = [D(item["weth_reserve"]) for item in observations]
    fresh_rpc_count = len([item for item in observations if item.get("provider") in RPC_URLS and not any(e.get("provider") == "all" for e in errors)])
    agree = fresh_rpc_count >= 3 and max(blocks) - min(blocks) <= 4 and max(tokens) == min(tokens) and max(weths) == min(weths)
    price = D(str(dex.get("priceUsd") or 0))
    now = int(time.time())
    state = {
        "block_number": max(blocks),
        "timestamp": max(item["timestamp"] for item in observations),
        "token_reserve": str(tokens[0]),
        "weth_reserve": str(weths[0]),
        "eth_usd": str(price * tokens[0] / weths[0]) if weths[0] else "0",
        "confidence": "observed" if agree else "invalid",
        "source": "scheduled public multi-RPC getReserves + DEX Screener",
    }
    snapshot = {
        "observed_at": now,
        "age_seconds": max(0, now - state["timestamp"]),
        "rpc_agreement": agree,
        "providers_ok": fresh_rpc_count,
        "providers_total": len(RPC_URLS),
        "observations": observations,
        "errors": errors,
        "state": state,
        "dex": {
            "price_usd": str(price),
            "liquidity_usd": str((dex.get("liquidity") or {}).get("usd") or 0),
            "volume_24h_usd": str((dex.get("volume") or {}).get("h24") or 0),
            "change_24h_pct": str((dex.get("priceChange") or {}).get("h24") or 0),
        },
    }
    heartbeat = {
        "status": "live" if fresh_rpc_count >= 3 and agree else "degraded",
        "rpc_ok": fresh_rpc_count,
        "rpc_agreement": agree,
        "age_seconds": snapshot["age_seconds"],
        "block": max(blocks),
        "dex_price_usd": str(price),
        "ts": now,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "live.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    (OUT / "heartbeat.json").write_text(json.dumps(heartbeat, indent=2) + "\n")


if __name__ == "__main__":
    main()
