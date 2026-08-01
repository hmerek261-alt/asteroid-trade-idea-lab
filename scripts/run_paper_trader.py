#!/usr/bin/env python3
"""Persistent, paper-only ASTEROID pump recycler.

No signer, private key, wallet adapter, transaction builder, or broadcast path
exists in this module. It consumes a market snapshot and writes research state.
"""
from __future__ import annotations

import json
import hashlib
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path

getcontext().prec = 40
D = Decimal
ROOT = Path(__file__).resolve().parents[1]
MARKET_FILE = ROOT / "docs" / "data" / "live.json"
STATE_FILE = ROOT / "docs" / "data" / "paper_trader.json"

TOTAL_TOKENS = D("74683073")
CORE_FLOOR = TOTAL_TOKENS * D("0.50")
SELL_TRANCHE = TOTAL_TOKENS * D("0.10")
FEE = D("0.003")
GAS_UNITS = D("150000")
MIN_LIQUIDITY_USD = D("250000")
MAX_IMPACT_BPS = D("200")
MIN_TOKEN_GAIN = D("0.04")
COOLDOWN_SECONDS = 30 * 60
MAX_ACTIONS_DAY = 6
MAX_CYCLES_DAY = 3
BUYBACK_LADDER = [(D("0.07"), D("0.25")), (D("0.11"), D("0.35")), (D("0.16"), D("0.40"))]
CONFIG = {
    "core_floor": "50%", "sell_tranche_total": "10%", "fee": str(FEE),
    "minimum_token_gain": str(MIN_TOKEN_GAIN), "maximum_impact_bps": str(MAX_IMPACT_BPS),
    "buyback_ladder": [[str(a), str(b)] for a, b in BUYBACK_LADDER],
    "buyback_requires_independent_reversal": True,
}
CONFIG_HASH = hashlib.sha256(json.dumps(CONFIG, sort_keys=True).encode()).hexdigest()[:12]


def dec(value) -> D:
    return D(str(value or 0))


def now_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def default_state(now: int, price: D) -> dict:
    return {
        "version": "aggressive_pump_recycler_v1",
        "paper_only": True,
        "execution_disabled": True,
        "evaluation_interval_minutes": 5,
        "state": "SCANNING",
        "started_at": now_iso(now),
        "updated_at": now_iso(now),
        "tokens": str(TOTAL_TOKENS),
        "cash_usd": "0",
        "core_floor_tokens": str(CORE_FLOOR),
        "start_tokens": str(TOTAL_TOKENS),
        "start_price_usd": str(price),
        "realized_pnl_usd": "0",
        "gas_paid_usd": "0",
        "fees_paid_usd": "0",
        "actions": 0,
        "completed_cycles": 0,
        "extra_tokens": "0",
        "last_action_at": 0,
        "day": datetime.fromtimestamp(now, timezone.utc).date().isoformat(),
        "actions_today": 0,
        "cycles_today": 0,
        "armed_peak_price": "0",
        "armed_at": 0,
        "average_sell_price": "0",
        "tokens_sold_cycle": "0",
        "cash_cycle_start": "0",
        "buyback_levels": [],
        "history": [],
        "ledger": [],
        "blockers": [],
        "missed_opportunity": {},
        "evaluation_ledger": [],
        "last_source_block": 0,
        "config_hash": CONFIG_HASH,
        "code_commit": os.getenv("GITHUB_SHA", "local")[:12],
    }


def closest_price(history: list[dict], target: int) -> D | None:
    prior = [point for point in history if int(point["ts"]) <= target]
    return dec(prior[-1]["price"]) if prior else None


def pct_return(price: D, old: D | None) -> D | None:
    return (price / old - 1) if old and old > 0 else None


def sell_fill(tokens_in: D, token_reserve: D, weth_reserve: D, eth_usd: D, gas_usd: D) -> dict:
    effective = tokens_in * (1 - FEE)
    weth_out = weth_reserve * effective / (token_reserve + effective)
    spot = weth_reserve / token_reserve
    execution = weth_out / tokens_in
    impact_bps = max(D(0), (spot - execution) / spot * D(10000))
    gross = weth_out * eth_usd
    fee_usd = tokens_in * spot * eth_usd * FEE
    return {"gross_usd": gross, "net_usd": gross - gas_usd, "fee_usd": fee_usd,
            "impact_bps": impact_bps, "fill_price": gross / tokens_in}


def buy_fill(cash: D, token_reserve: D, weth_reserve: D, eth_usd: D, gas_usd: D) -> dict:
    spend = max(D(0), cash - gas_usd)
    weth_in = spend / eth_usd if eth_usd else D(0)
    effective = weth_in * (1 - FEE)
    tokens_out = token_reserve * effective / (weth_reserve + effective) if effective else D(0)
    spot = token_reserve / weth_reserve
    execution = tokens_out / weth_in if weth_in else D(0)
    impact_bps = max(D(0), (spot - execution) / spot * D(10000)) if spot else D(0)
    fee_usd = spend * FEE
    return {"tokens_out": tokens_out, "spent_usd": cash, "fee_usd": fee_usd,
            "impact_bps": impact_bps, "fill_price": spend / tokens_out if tokens_out else D(0)}


def ledger_entry(state: dict, now: int, side: str, price: D, tokens: D, cash: D,
                 gas: D, fee: D, impact: D, reasons: list[str]) -> dict:
    sequence = len(state["ledger"]) + 1
    return {
        "trade_id": f"AST-{datetime.fromtimestamp(now, timezone.utc):%Y%m%d}-{sequence:03d}",
        "strategy": state["version"], "side": side, "signal_time": now_iso(now),
        "fill_time": now_iso(now), "reference_price": str(price), "simulated_fill_price": str(price),
        "tokens": str(tokens), "cash_usd": str(cash), "swap_fee_usd": str(fee),
        "gas_usd": str(gas), "price_impact_bps": str(impact), "reason": reasons,
    }


def main() -> None:
    started_wall = int(time.time())
    market = json.loads(MARKET_FILE.read_text())
    now = int(market.get("observed_at") or time.time())
    price = dec((market.get("dex") or {}).get("price_usd"))
    liquidity = dec((market.get("dex") or {}).get("liquidity_usd"))
    observations = market.get("observations") or []
    observation = observations[0] if observations else {}
    token_reserve = dec(observation.get("token_reserve") or (market.get("state") or {}).get("token_reserve"))
    weth_reserve = dec(observation.get("weth_reserve") or (market.get("state") or {}).get("weth_reserve"))
    eth_usd = dec((market.get("state") or {}).get("eth_usd"))
    gas_gwei = dec(observation.get("gas_gwei"))
    gas_usd = gas_gwei * D("1e-9") * GAS_UNITS * eth_usd
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else default_state(now, price)
    state.setdefault("evaluation_ledger", [])
    state["config_hash"] = CONFIG_HASH
    state["code_commit"] = os.getenv("GITHUB_SHA", state.get("code_commit", "local"))[:12]
    source_block = int((market.get("state") or {}).get("block_number") or observation.get("block") or 0)
    state_before_hash = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]
    already_evaluated = source_block > 0 and any(int(e.get("source_block") or 0) == source_block for e in state["evaluation_ledger"])
    evaluation_decision = "ABSTAIN"
    evaluation_reasons = []

    today = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    if state.get("day") != today:
        state.update({"day": today, "actions_today": 0, "cycles_today": 0})

    history = state.setdefault("history", [])
    if not history or int(history[-1]["ts"]) < now:
        history.append({"ts": now, "price": str(price), "liquidity_usd": str(liquidity)})
    state["history"] = history[-2016:]

    returns = {}
    for label, seconds in (("20m", 1200), ("1h", 3600), ("4h", 14400), ("6h", 21600), ("24h", 86400)):
        value = pct_return(price, closest_price(history, now - seconds))
        returns[label] = str(value) if value is not None else None

    recent_hour = [dec(p["price"]) for p in history if int(p["ts"]) >= now - 3600]
    session = [dec(p["price"]) for p in history if datetime.fromtimestamp(int(p["ts"]), timezone.utc).date().isoformat() == today]
    hour_high = max(recent_hour) if recent_hour else price
    session_high = max(session) if session else price
    session_low_after_high = min(session[session.index(session_high):]) if session else price
    rejection = price <= hour_high * D("0.995")
    recent_prices = [dec(point["price"]) for point in history[-4:]]
    initial_reversal = len(recent_prices) >= 3 and recent_prices[-1] > recent_prices[-2] <= recent_prices[-3]
    reversal_confirmed = len(recent_prices) >= 4 and recent_prices[-1] > recent_prices[-2] > recent_prices[-3]
    fast_pump = returns["20m"] is not None and dec(returns["20m"]) >= D("0.07")
    pump_1h = returns["1h"] is not None and dec(returns["1h"]) >= D("0.10")
    pump_4h = returns["4h"] is not None and dec(returns["4h"]) >= D("0.14")
    pump_detected = fast_pump or pump_1h or pump_4h

    blockers = []
    if now - int(market.get("observed_at") or 0) > 900: blockers.append("market_snapshot_stale")
    if not market.get("rpc_agreement"): blockers.append("rpc_disagreement_or_fallback")
    if liquidity < MIN_LIQUIDITY_USD: blockers.append("liquidity_below_minimum")
    if now - int(state.get("last_action_at") or 0) < COOLDOWN_SECONDS: blockers.append("cooldown_active")
    if int(state.get("actions_today", 0)) >= MAX_ACTIONS_DAY: blockers.append("daily_action_limit")
    if int(state.get("cycles_today", 0)) >= MAX_CYCLES_DAY: blockers.append("daily_cycle_limit")
    if already_evaluated: blockers.append("duplicate_source_block")
    state["blockers"] = blockers

    current_state = state.get("state", "SCANNING")
    if current_state == "SCANNING" and pump_detected and not blockers:
        state.update({"state": "SELL_ARMED", "armed_peak_price": str(hour_high), "armed_at": now})
        evaluation_decision = "ARM_SELL"; evaluation_reasons = ["adaptive_pump_detected"]
    elif current_state == "SELL_ARMED":
        peak = max(dec(state.get("armed_peak_price")), price)
        state["armed_peak_price"] = str(peak)
        if rejection and not blockers:
            available = max(D(0), dec(state["tokens"]) - CORE_FLOOR)
            quantity = min(SELL_TRANCHE, available)
            fill = sell_fill(quantity, token_reserve, weth_reserve, eth_usd, gas_usd)
            if quantity > 0 and fill["impact_bps"] <= MAX_IMPACT_BPS and fill["net_usd"] > 0:
                state["tokens"] = str(dec(state["tokens"]) - quantity)
                state["cash_usd"] = str(dec(state["cash_usd"]) + fill["net_usd"])
                state["average_sell_price"] = str(fill["fill_price"])
                state["tokens_sold_cycle"] = str(quantity)
                state["cash_cycle_start"] = str(fill["net_usd"])
                state["buyback_levels"] = [
                    {"drop_pct": str(drop), "cash_fraction": str(fraction),
                     "trigger_price": str(fill["fill_price"] * (1 - drop)), "filled": False}
                    for drop, fraction in BUYBACK_LADDER
                ]
                state["ledger"].append(ledger_entry(state, now, "SELL", fill["fill_price"], quantity,
                                                     fill["net_usd"], gas_usd, fill["fee_usd"],
                                                     fill["impact_bps"], ["adaptive_pump", "price_rejection"]))
                state["actions"] += 1; state["actions_today"] += 1; state["last_action_at"] = now
                state["gas_paid_usd"] = str(dec(state["gas_paid_usd"]) + gas_usd)
                state["fees_paid_usd"] = str(dec(state["fees_paid_usd"]) + fill["fee_usd"])
                state["state"] = "CASH_HELD"
                evaluation_decision = "PAPER_SELL"; evaluation_reasons = ["adaptive_pump", "price_rejection"]
            else:
                state["state"] = "PAUSED_BY_RISK"; state["blockers"].append("sell_fill_failed_risk_gate")
        elif now - int(state.get("armed_at") or now) > 21600:
            state.update({"state": "SCANNING", "armed_peak_price": "0", "armed_at": 0})
    elif current_state in {"CASH_HELD", "RETRACE_DETECTED", "STABILIZATION_WATCH", "PARTIALLY_BOUGHT_BACK", "WAITING_FOR_BUYBACK"}:
        cycle_cash = dec(state.get("cash_cycle_start"))
        eligible_levels = [level for level in state.get("buyback_levels", []) if not level.get("filled") and price <= dec(level["trigger_price"])]
        if current_state in {"CASH_HELD", "WAITING_FOR_BUYBACK"} and eligible_levels:
            state["state"] = "RETRACE_DETECTED"
            evaluation_decision = "HOLD_CASH"; evaluation_reasons = ["retrace_detected", "independent_reversal_not_yet_confirmed"]
        elif current_state == "RETRACE_DETECTED":
            if initial_reversal:
                state["state"] = "STABILIZATION_WATCH"
                evaluation_decision = "HOLD_CASH"; evaluation_reasons = ["initial_reversal_seen", "second_confirmation_required"]
            else:
                evaluation_reasons = ["waiting_for_reversal_confirmation"]
        elif current_state == "STABILIZATION_WATCH" and not reversal_confirmed:
            state["state"] = "RETRACE_DETECTED"
            evaluation_reasons = ["reversal_confirmation_failed"]
        elif current_state in {"STABILIZATION_WATCH", "PARTIALLY_BOUGHT_BACK"} and reversal_confirmed:
            evaluation_decision = "HOLD_CASH"; evaluation_reasons = ["reversal_confirmed", "testing_buyback_cost_gate"]
        else:
            eligible_levels = []
        for level in state.get("buyback_levels", []):
            if level.get("filled") or level not in eligible_levels or blockers or state["state"] not in {"STABILIZATION_WATCH", "PARTIALLY_BOUGHT_BACK"} or not reversal_confirmed:
                continue
            allocation = min(dec(state["cash_usd"]), cycle_cash * dec(level["cash_fraction"]))
            fill = buy_fill(allocation, token_reserve, weth_reserve, eth_usd, gas_usd)
            target_tokens = dec(state["tokens_sold_cycle"]) * dec(level["cash_fraction"])
            if fill["tokens_out"] >= target_tokens * (1 + MIN_TOKEN_GAIN) and fill["impact_bps"] <= MAX_IMPACT_BPS:
                state["tokens"] = str(dec(state["tokens"]) + fill["tokens_out"])
                state["cash_usd"] = str(dec(state["cash_usd"]) - allocation)
                level.update({"filled": True, "filled_at": now_iso(now), "tokens_received": str(fill["tokens_out"])})
                state["ledger"].append(ledger_entry(state, now, "BUY", fill["fill_price"], fill["tokens_out"],
                                                     allocation, gas_usd, fill["fee_usd"], fill["impact_bps"],
                                                     [f"buyback_{level['drop_pct']}", "net_token_gain_gate_passed"]))
                state["actions"] += 1; state["actions_today"] += 1; state["last_action_at"] = now
                state["gas_paid_usd"] = str(dec(state["gas_paid_usd"]) + gas_usd)
                state["fees_paid_usd"] = str(dec(state["fees_paid_usd"]) + fill["fee_usd"])
                state["state"] = "PARTIALLY_BOUGHT_BACK"
                evaluation_decision = "PAPER_BUYBACK"; evaluation_reasons = [f"buyback_{level['drop_pct']}", "independent_reversal_confirmed", "net_token_gain_gate_passed"]
                break
        if state.get("buyback_levels") and all(level.get("filled") for level in state["buyback_levels"]):
            gain = dec(state["tokens"]) - TOTAL_TOKENS
            state["extra_tokens"] = str(gain)
            state["completed_cycles"] += 1; state["cycles_today"] += 1
            state.update({"state": "CYCLE_COMPLETE", "average_sell_price": "0", "tokens_sold_cycle": "0",
                          "cash_cycle_start": "0", "buyback_levels": []})
    elif current_state == "CYCLE_COMPLETE":
        state["state"] = "SCANNING"
    elif current_state == "PAUSED_BY_RISK" and not blockers:
        state["state"] = "SCANNING"

    equity = dec(state["tokens"]) * price + dec(state["cash_usd"])
    hold_equity = TOTAL_TOKENS * price
    state["updated_at"] = now_iso(now)
    state["market"] = {"price_usd": str(price), "liquidity_usd": str(liquidity),
                       "gas_usd": str(gas_usd), "returns": returns, "rpc_agreement": bool(market.get("rpc_agreement"))}
    state["equity_usd"] = str(equity)
    state["hold_equity_usd"] = str(hold_equity)
    state["vs_hold_usd"] = str(equity - hold_equity)
    state["extra_tokens"] = str(dec(state["tokens"]) - TOTAL_TOKENS)
    retracement = (session_low_after_high / session_high - 1) if session_high else D(0)
    state["missed_opportunity"] = {
        "session_high": str(session_high), "session_low_after_high": str(session_low_after_high),
        "maximum_retracement_pct": str(retracement * 100), "pump_detected": pump_detected,
        "sell_signal_armed": state["state"] in {"SELL_ARMED", "CASH_HELD", "RETRACE_DETECTED", "STABILIZATION_WATCH", "PARTIALLY_BOUGHT_BACK"},
        "primary_blocker": state["blockers"][0] if state["blockers"] else None,
        "classification": "RISK BLOCKED" if state["blockers"] and pump_detected else "MONITORING",
    }
    state["ledger"] = state["ledger"][-500:]
    if not evaluation_reasons:
        evaluation_reasons = list(state.get("blockers") or []) or ["no_qualified_signal"]
    if not already_evaluated:
        state_after_hash = hashlib.sha256(json.dumps({k: v for k, v in state.items() if k != "evaluation_ledger"}, sort_keys=True).encode()).hexdigest()[:16]
        state["evaluation_ledger"].append({
            "scheduled_at": None,
            "started_at": now_iso(started_wall),
            "completed_at": now_iso(int(time.time())),
            "source_block": source_block,
            "data_status": "valid" if not any(x in blockers for x in ("market_snapshot_stale", "rpc_disagreement_or_fallback", "liquidity_below_minimum")) else "blocked",
            "signal_status": "pump" if pump_detected else "none",
            "risk_status": "blocked" if blockers else "passed",
            "decision": evaluation_decision,
            "reason_codes": evaluation_reasons,
            "engine_version": state["version"],
            "config_hash": CONFIG_HASH,
            "code_commit": state["code_commit"],
            "state_before_hash": state_before_hash,
            "state_after_hash": state_after_hash,
        })
        state["last_source_block"] = source_block
    state["evaluation_ledger"] = state["evaluation_ledger"][-1000:]
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


if __name__ == "__main__":
    main()
