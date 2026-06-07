#!/usr/bin/env python3
"""Latency diagnostics for PolyBot network routes.

This script is read-only. It does not authenticate and does not place orders.
It measures the public endpoints used around live trading:

- Polymarket CLOB health, price, and book endpoints
- Polymarket Gamma current-market discovery
- Binance REST price endpoint
- Binance trade WebSocket connect and first-message latency
"""

import argparse
import asyncio
import json
import os
import statistics
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


DEFAULT_CLOB_API_URL = "https://clob.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"
BINANCE_REST_PING_URL = "https://api.binance.com/api/v3/ping"
BINANCE_REST_PRICE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
PERIOD_SECONDS = {5: 300, 15: 900}


@dataclass
class CheckResult:
    name: str
    samples_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.samples_ms) and not self.errors

    @property
    def avg(self) -> float:
        return statistics.mean(self.samples_ms) if self.samples_ms else 0.0

    @property
    def minimum(self) -> float:
        return min(self.samples_ms) if self.samples_ms else 0.0

    @property
    def maximum(self) -> float:
        return max(self.samples_ms) if self.samples_ms else 0.0

    @property
    def p95(self) -> float:
        if not self.samples_ms:
            return 0.0
        vals = sorted(self.samples_ms)
        idx = min(len(vals) - 1, int(round((len(vals) - 1) * 0.95)))
        return vals[idx]


def now_ms() -> int:
    return int(time.time() * 1000)


def current_window_ts(period_minutes: int) -> int:
    period = PERIOD_SECONDS.get(period_minutes, 300)
    now = int(time.time())
    return now - (now % period)


def http_get_json(url: str, timeout: float) -> tuple[Any, int, float]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "PolyBot-LatencyCheck/1.0",
        },
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        elapsed_ms = (time.perf_counter() - started) * 1000
        body = raw.decode("utf-8", errors="replace")
        if not body.strip():
            data = {}
        else:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = body
        return data, int(getattr(resp, "status", 0) or 0), elapsed_ms


def sample_http(name: str, url: str, samples: int, timeout: float) -> CheckResult:
    result = CheckResult(name=name)
    for _ in range(samples):
        try:
            _, status, elapsed_ms = http_get_json(url, timeout)
            if 200 <= status < 300:
                result.samples_ms.append(elapsed_ms)
            else:
                result.errors.append(f"HTTP {status}")
        except Exception as exc:
            result.errors.append(str(exc))
        time.sleep(0.15)
    return result


def discover_current_market(period_minutes: int, timeout: float) -> tuple[str, str, str]:
    slug = f"btc-updown-{period_minutes}m-{current_window_ts(period_minutes)}"
    url = f"{GAMMA_API_URL}/events?slug={urllib.parse.quote(slug)}"
    data, status, _ = http_get_json(url, timeout)
    if status < 200 or status >= 300 or not isinstance(data, list) or not data:
        return slug, "", ""

    markets = data[0].get("markets", [])
    if not markets:
        return slug, "", ""

    market = markets[0]
    token_ids = market.get("clobTokenIds", [])
    outcomes = market.get("outcomes", [])
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)

    up_token = ""
    down_token = ""
    for idx, outcome in enumerate(outcomes):
        if idx >= len(token_ids):
            continue
        outcome_name = str(outcome).lower()
        if outcome_name == "up":
            up_token = str(token_ids[idx])
        elif outcome_name == "down":
            down_token = str(token_ids[idx])

    if not up_token and len(token_ids) >= 1:
        up_token = str(token_ids[0])
    if not down_token and len(token_ids) >= 2:
        down_token = str(token_ids[1])

    return slug, up_token, down_token


async def sample_binance_ws(samples: int, timeout: float) -> list[CheckResult]:
    connect_result = CheckResult(name="binance.ws.connect")
    first_trade_result = CheckResult(name="binance.ws.first_trade")
    exchange_lag_result = CheckResult(name="binance.ws.exchange_lag", note="local clock dependent")

    try:
        import websockets
    except ImportError:
        msg = "websockets package is not installed"
        connect_result.errors.append(msg)
        first_trade_result.errors.append(msg)
        exchange_lag_result.errors.append(msg)
        return [connect_result, first_trade_result, exchange_lag_result]

    for _ in range(samples):
        try:
            started = time.perf_counter()
            async with websockets.connect(
                BINANCE_WS_URL,
                open_timeout=timeout,
                ping_interval=None,
                close_timeout=1,
            ) as ws:
                connected = time.perf_counter()
                message = await asyncio.wait_for(ws.recv(), timeout=timeout)
                first_msg = time.perf_counter()

            connect_result.samples_ms.append((connected - started) * 1000)
            first_trade_result.samples_ms.append((first_msg - started) * 1000)

            data = json.loads(message)
            event_time = int(data.get("E", 0) or 0)
            if event_time > 0:
                exchange_lag_result.samples_ms.append(max(0.0, now_ms() - event_time))
        except Exception as exc:
            err = str(exc)
            connect_result.errors.append(err)
            first_trade_result.errors.append(err)
        await asyncio.sleep(0.15)

    return [connect_result, first_trade_result, exchange_lag_result]


def grade(avg_ms: float) -> str:
    if avg_ms <= 0:
        return "FAIL"
    if avg_ms <= 150:
        return "FAST"
    if avg_ms <= 350:
        return "OK"
    if avg_ms <= 700:
        return "SLOW"
    return "BAD"


def print_results(results: list[CheckResult]):
    print("\nResults")
    print("-" * 88)
    print(f"{'check':32} {'avg':>8} {'p95':>8} {'min':>8} {'max':>8} {'grade':>7}  note")
    print("-" * 88)
    for r in results:
        if r.samples_ms:
            note = r.note
            if r.errors:
                note = (note + " | " if note else "") + f"errors={len(r.errors)}"
            print(
                f"{r.name:32} "
                f"{r.avg:7.0f}ms "
                f"{r.p95:7.0f}ms "
                f"{r.minimum:7.0f}ms "
                f"{r.maximum:7.0f}ms "
                f"{grade(r.avg):>7}  {note}"
            )
        else:
            err = r.errors[0] if r.errors else "no samples"
            print(f"{r.name:32} {'-':>8} {'-':>8} {'-':>8} {'-':>8} {'FAIL':>7}  {err}")
    print("-" * 88)


def main():
    if load_dotenv:
        load_dotenv()

    parser = argparse.ArgumentParser(description="Check PolyBot API and feed latency.")
    parser.add_argument("--samples", type=int, default=int(os.getenv("LATENCY_SAMPLES", "5")))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("LATENCY_TIMEOUT", "5")))
    parser.add_argument("--period", type=int, default=int(os.getenv("MARKET_PERIOD", "5")))
    parser.add_argument("--skip-ws", action="store_true", help="Skip Binance WebSocket check.")
    args = parser.parse_args()

    samples = max(1, args.samples)
    timeout = max(1.0, args.timeout)
    clob_url = os.getenv("CLOB_API_URL", DEFAULT_CLOB_API_URL).rstrip("/")

    print("PolyBot Latency Check")
    print("=" * 88)
    print(f"samples={samples} timeout={timeout:.1f}s clob={clob_url}")

    slug, up_token, down_token = discover_current_market(args.period, timeout)
    print(f"market={slug}")
    if up_token and down_token:
        print(f"tokens=up:{up_token[:10]}... down:{down_token[:10]}...")
    else:
        print("tokens=not found; CLOB price/book checks will be skipped")

    endpoints = [
        ("polymarket.clob.ok", f"{clob_url}/ok"),
        ("polymarket.gamma.market", f"{GAMMA_API_URL}/events?slug={urllib.parse.quote(slug)}"),
        ("binance.rest.ping", BINANCE_REST_PING_URL),
        ("binance.rest.price", BINANCE_REST_PRICE_URL),
    ]

    if up_token:
        endpoints.extend(
            [
                (
                    "polymarket.clob.price.up",
                    f"{clob_url}/price?token_id={urllib.parse.quote(up_token)}&side=BUY",
                ),
                (
                    "polymarket.clob.book.up",
                    f"{clob_url}/book?token_id={urllib.parse.quote(up_token)}",
                ),
            ]
        )
    if down_token:
        endpoints.append(
            (
                "polymarket.clob.price.down",
                f"{clob_url}/price?token_id={urllib.parse.quote(down_token)}&side=BUY",
            )
        )

    results = [sample_http(name, url, samples, timeout) for name, url in endpoints]
    if not args.skip_ws:
        results.extend(asyncio.run(sample_binance_ws(samples, timeout)))

    print_results(results)
    print("Rule of thumb: for this bot, CLOB price/book and Binance WS should stay under ~350ms avg.")


if __name__ == "__main__":
    main()
