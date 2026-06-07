#!/usr/bin/env python3
"""Follow and analyze a public Polymarket trader.

This is a read-only research tool. It does not authenticate, does not use a
wallet, and does not place orders. It polls public Polymarket Data/Gamma API
endpoints, logs new trades and current positions, and keeps per-market
inventory summaries so we can study laddering/hedging behavior.
"""

import argparse
import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_PRICE_API = "https://api.binance.com/api/v3/ticker/price"
DEFAULT_USERNAME = "jetfadil"
DEFAULT_WALLET = "0xe0229e10a858860218b6132f4234602c47bd6603"
SNAPSHOT_FAMILIES = {
    "btc5m": ("btc-updown-5m", 300, "BTCUSDT"),
    "eth5m": ("eth-updown-5m", 300, "ETHUSDT"),
    "btc15m": ("btc-updown-15m", 900, "BTCUSDT"),
    "eth15m": ("eth-updown-15m", 900, "ETHUSDT"),
}


def utc_iso(ts: float | int | None = None) -> str:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def http_json_timed(
    url: str,
    params: dict[str, Any] | None = None,
    timeout: float = 10,
) -> tuple[Any, float]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "PolyBot-Research/1.0"})
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    elapsed_ms = (time.perf_counter() - started) * 1000
    return (json.loads(raw) if raw else {}), elapsed_ms


def http_json(url: str, params: dict[str, Any] | None = None, timeout: float = 10) -> Any:
    data, _ = http_json_timed(url, params=params, timeout=timeout)
    return data


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def ensure_csv(path: Path, fieldnames: list[str]):
    if path.exists() and path.stat().st_size > 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()


def append_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]):
    if not rows:
        return
    ensure_csv(path, fieldnames)
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writerows(rows)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def append_jsonl(path: Path, rows: list[dict[str, Any]]):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen_trades": [], "markets": {}, "positions": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"seen_trades": [], "markets": {}, "positions": {}}


def save_state(path: Path, state: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    state["saved_at"] = utc_iso()
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def resolve_wallet(username: str, wallet: str = "") -> tuple[str, str]:
    if wallet:
        return username, wallet.lower()
    if username.startswith("0x") and len(username) >= 42:
        return username, username.lower()

    data = http_json(
        f"{GAMMA_API}/public-search",
        {"q": username, "search_profiles": "true", "limit_per_type": 10},
    )
    profiles = data.get("profiles", []) if isinstance(data, dict) else []
    if not profiles:
        return username, DEFAULT_WALLET

    exact = None
    for profile in profiles:
        if str(profile.get("name", "")).lower() == username.lower():
            exact = profile
            break
    chosen = exact or profiles[0]
    name = chosen.get("name", username)
    proxy_wallet = chosen.get("proxyWallet") or DEFAULT_WALLET
    return name, str(proxy_wallet).lower()


def fetch_trades(wallet: str, limit: int) -> list[dict[str, Any]]:
    data = http_json(f"{DATA_API}/trades", {"user": wallet, "limit": limit})
    return data if isinstance(data, list) else []


def fetch_positions(wallet: str, limit: int) -> list[dict[str, Any]]:
    data = http_json(
        f"{DATA_API}/positions",
        {"user": wallet, "limit": limit, "sizeThreshold": 0},
    )
    return data if isinstance(data, list) else []


def current_window_start(period_seconds: int) -> int:
    now = int(time.time())
    return now - (now % period_seconds)


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def fetch_market_tokens(slug: str, timeout: float) -> dict[str, Any]:
    data, elapsed_ms = http_json_timed(f"{GAMMA_API}/events", {"slug": slug}, timeout=timeout)
    if not isinstance(data, list) or not data:
        return {"slug": slug, "gamma_ms": elapsed_ms, "error": "gamma market not found"}
    event = data[0]
    markets = event.get("markets", [])
    if not markets:
        return {"slug": slug, "gamma_ms": elapsed_ms, "error": "gamma event has no markets"}

    market = markets[0]
    token_ids = parse_json_list(market.get("clobTokenIds", []))
    outcomes = parse_json_list(market.get("outcomes", []))
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

    return {
        "slug": slug,
        "title": market.get("question") or event.get("title") or slug,
        "condition_id": market.get("conditionId") or market.get("condition_id") or "",
        "up_token": up_token,
        "down_token": down_token,
        "gamma_ms": elapsed_ms,
        "error": "" if up_token and down_token else "missing clob token ids",
    }


def fetch_clob_price_timed(
    clob_url: str,
    token_id: str,
    side: str,
    timeout: float,
) -> tuple[float, float]:
    if not token_id:
        return 0.0, 0.0
    data, elapsed_ms = http_json_timed(
        f"{clob_url.rstrip('/')}/price",
        {"token_id": token_id, "side": side.upper()},
        timeout=timeout,
    )
    if isinstance(data, dict):
        return as_float(data.get("price")), elapsed_ms
    return as_float(data), elapsed_ms


def fetch_clob_price(clob_url: str, token_id: str, side: str, timeout: float) -> float:
    price, _ = fetch_clob_price_timed(clob_url, token_id, side, timeout)
    return price


def best_book_prices_timed(
    clob_url: str,
    token_id: str,
    timeout: float,
) -> tuple[float, float, float]:
    if not token_id:
        return 0.0, 0.0, 0.0
    data, elapsed_ms = http_json_timed(
        f"{clob_url.rstrip('/')}/book",
        {"token_id": token_id},
        timeout=timeout,
    )
    if not isinstance(data, dict):
        return 0.0, 0.0, elapsed_ms
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    bid_prices = [as_float(level.get("price")) for level in bids if isinstance(level, dict)]
    ask_prices = [as_float(level.get("price")) for level in asks if isinstance(level, dict)]
    bid_prices = [p for p in bid_prices if p > 0]
    ask_prices = [p for p in ask_prices if p > 0]
    best_bid = max(bid_prices) if bid_prices else 0.0
    best_ask = min(ask_prices) if ask_prices else 0.0
    return best_bid, best_ask, elapsed_ms


def best_book_prices(clob_url: str, token_id: str, timeout: float) -> tuple[float, float]:
    best_bid, best_ask, _ = best_book_prices_timed(clob_url, token_id, timeout)
    return best_bid, best_ask


def fetch_symbol_price_timed(symbol: str, timeout: float) -> tuple[float, float]:
    if not symbol:
        return 0.0, 0.0
    data, elapsed_ms = http_json_timed(BINANCE_PRICE_API, {"symbol": symbol}, timeout=timeout)
    if isinstance(data, dict):
        return as_float(data.get("price")), elapsed_ms
    return 0.0, elapsed_ms


def fetch_symbol_price(symbol: str, timeout: float) -> float:
    price, _ = fetch_symbol_price_timed(symbol, timeout)
    return price


def trade_key(trade: dict[str, Any]) -> str:
    return "|".join(
        str(trade.get(k, ""))
        for k in ("transactionHash", "asset", "side", "timestamp", "price", "size")
    )


def market_family(slug: str) -> str:
    s = slug.lower()
    if "btc-updown-5m" in s:
        return "btc5m"
    if "btc-updown-15m" in s:
        return "btc15m"
    if "eth-updown-5m" in s:
        return "eth5m"
    if "eth-updown-15m" in s:
        return "eth15m"
    if "sol" in s:
        return "sol"
    if "xrp" in s:
        return "xrp"
    return "other"


def parse_crypto_window(slug: str) -> tuple[int, int]:
    match = re.search(r"-(5m|15m)-(\d{10})$", slug)
    if not match:
        return 0, 0
    period = 300 if match.group(1) == "5m" else 900
    start = int(match.group(2))
    return start, start + period


def seconds_to_close(slug: str, timestamp: float) -> float:
    _, end_ts = parse_crypto_window(slug)
    if end_ts <= 0:
        return 0.0
    return end_ts - float(timestamp)


def seconds_from_open(slug: str, timestamp: float) -> float:
    start_ts, _ = parse_crypto_window(slug)
    if start_ts <= 0:
        return 0.0
    return float(timestamp) - start_ts


def price_band(price: float) -> str:
    if price < 0.05:
        return "<5c"
    if price < 0.20:
        return "5-20c"
    if price < 0.50:
        return "20-50c"
    if price < 0.80:
        return "50-80c"
    if price < 0.95:
        return "80-95c"
    return "95c+"


def normalize_trade(trade: dict[str, Any]) -> dict[str, Any]:
    price = float(trade.get("price") or 0)
    size = float(trade.get("size") or 0)
    timestamp = float(trade.get("timestamp") or 0)
    slug = str(trade.get("slug") or "")
    return {
        "seen_at": utc_iso(),
        "trade_time": utc_iso(timestamp) if timestamp else "",
        "timestamp": int(timestamp) if timestamp else 0,
        "seconds_from_open": round(seconds_from_open(slug, timestamp), 3),
        "seconds_to_close": round(seconds_to_close(slug, timestamp), 3),
        "family": market_family(slug),
        "side": trade.get("side", ""),
        "slug": slug,
        "event_slug": trade.get("eventSlug", ""),
        "title": trade.get("title", ""),
        "outcome": trade.get("outcome", ""),
        "outcome_index": trade.get("outcomeIndex", ""),
        "price": price,
        "size": size,
        "notional": round(price * size, 6),
        "price_band": price_band(price),
        "asset": trade.get("asset", ""),
        "condition_id": trade.get("conditionId", ""),
        "tx_hash": trade.get("transactionHash", ""),
    }


def update_market(markets: dict[str, Any], row: dict[str, Any]):
    slug = row["slug"]
    market = markets.setdefault(
        slug,
        {
            "slug": slug,
            "family": row["family"],
            "title": row["title"],
            "event_slug": row["event_slug"],
            "first_ts": row["timestamp"],
            "last_ts": row["timestamp"],
            "trades": 0,
            "outcomes": {},
            "price_bands": {},
        },
    )
    market["first_ts"] = min(int(market.get("first_ts") or row["timestamp"]), row["timestamp"])
    market["last_ts"] = max(int(market.get("last_ts") or row["timestamp"]), row["timestamp"])
    market["trades"] = int(market.get("trades") or 0) + 1
    market["title"] = row["title"] or market.get("title", "")
    market["family"] = row["family"]
    band_counts = market.setdefault("price_bands", {})
    band_counts[row["price_band"]] = int(band_counts.get(row["price_band"], 0)) + 1

    outcome = row["outcome"] or f"idx_{row['outcome_index']}"
    out = market.setdefault("outcomes", {}).setdefault(
        outcome,
        {
            "buy_count": 0,
            "sell_count": 0,
            "buy_shares": 0.0,
            "sell_shares": 0.0,
            "buy_cost": 0.0,
            "sell_value": 0.0,
            "first_ts": row["timestamp"],
            "last_ts": row["timestamp"],
        },
    )
    out["first_ts"] = min(int(out.get("first_ts") or row["timestamp"]), row["timestamp"])
    out["last_ts"] = max(int(out.get("last_ts") or row["timestamp"]), row["timestamp"])
    if str(row["side"]).upper() == "SELL":
        out["sell_count"] += 1
        out["sell_shares"] += row["size"]
        out["sell_value"] += row["notional"]
    else:
        out["buy_count"] += 1
        out["buy_shares"] += row["size"]
        out["buy_cost"] += row["notional"]


def summarize_market(slug: str, market: dict[str, Any]) -> dict[str, Any]:
    outcomes = market.get("outcomes", {})
    outcome_rows = []
    for outcome, stats in outcomes.items():
        buy_shares = float(stats.get("buy_shares") or 0)
        buy_cost = float(stats.get("buy_cost") or 0)
        avg_buy = buy_cost / buy_shares if buy_shares > 0 else 0.0
        outcome_rows.append(
            {
                "outcome": outcome,
                "buy_shares": buy_shares,
                "buy_cost": buy_cost,
                "avg_buy": avg_buy,
                "buy_count": int(stats.get("buy_count") or 0),
                "sell_count": int(stats.get("sell_count") or 0),
            }
        )
    outcome_rows.sort(key=lambda x: x["buy_cost"], reverse=True)

    both_sides = len([r for r in outcome_rows if r["buy_shares"] > 0]) > 1
    primary = outcome_rows[0] if outcome_rows else {}
    secondary = outcome_rows[1] if len(outcome_rows) > 1 else {}
    primary_cost = float(primary.get("buy_cost") or 0)
    secondary_cost = float(secondary.get("buy_cost") or 0)
    primary_shares = float(primary.get("buy_shares") or 0)
    secondary_shares = float(secondary.get("buy_shares") or 0)
    matched_pairs = min(primary_shares, secondary_shares) if both_sides else 0.0
    hedge_ratio_cost = secondary_cost / primary_cost if primary_cost > 0 and both_sides else 0.0
    hedge_ratio_shares = secondary_shares / primary_shares if primary_shares > 0 and both_sides else 0.0
    pair_cost_est = (
        float(primary.get("avg_buy") or 0) + float(secondary.get("avg_buy") or 0)
        if both_sides
        else 0.0
    )

    first_ts = int(market.get("first_ts") or 0)
    last_ts = int(market.get("last_ts") or 0)
    return {
        "updated_at": utc_iso(),
        "slug": slug,
        "family": market.get("family", ""),
        "title": market.get("title", ""),
        "trades": int(market.get("trades") or 0),
        "both_sides": both_sides,
        "outcome_count": len(outcome_rows),
        "first_trade_time": utc_iso(first_ts) if first_ts else "",
        "last_trade_time": utc_iso(last_ts) if last_ts else "",
        "entry_span_seconds": max(0, last_ts - first_ts) if first_ts and last_ts else 0,
        "primary_outcome": primary.get("outcome", ""),
        "primary_cost": round(primary_cost, 6),
        "primary_shares": round(primary_shares, 6),
        "primary_avg": round(float(primary.get("avg_buy") or 0), 6),
        "secondary_outcome": secondary.get("outcome", ""),
        "secondary_cost": round(secondary_cost, 6),
        "secondary_shares": round(secondary_shares, 6),
        "secondary_avg": round(float(secondary.get("avg_buy") or 0), 6),
        "matched_pairs_est": round(matched_pairs, 6),
        "pair_cost_est": round(pair_cost_est, 6),
        "hedge_ratio_cost": round(hedge_ratio_cost, 6),
        "hedge_ratio_shares": round(hedge_ratio_shares, 6),
        "net_outcome": primary.get("outcome", ""),
        "net_shares_est": round(max(0.0, primary_shares - secondary_shares), 6),
        "total_buy_cost": round(sum(r["buy_cost"] for r in outcome_rows), 6),
        "price_bands": json.dumps(market.get("price_bands", {}), sort_keys=True),
    }


def normalize_position(pos: dict[str, Any]) -> dict[str, Any]:
    return {
        "seen_at": utc_iso(),
        "slug": pos.get("slug", ""),
        "family": market_family(str(pos.get("slug", ""))),
        "title": pos.get("title", ""),
        "outcome": pos.get("outcome", ""),
        "size": float(pos.get("size") or 0),
        "avg_price": float(pos.get("avgPrice") or 0),
        "cur_price": float(pos.get("curPrice") or 0),
        "current_value": float(pos.get("currentValue") or 0),
        "cash_pnl": float(pos.get("cashPnl") or 0),
        "percent_pnl": float(pos.get("percentPnl") or 0),
        "mergeable": bool(pos.get("mergeable")),
        "redeemable": bool(pos.get("redeemable")),
        "asset": pos.get("asset", ""),
    }


TRADE_FIELDS = [
    "seen_at",
    "trade_time",
    "timestamp",
    "seconds_from_open",
    "seconds_to_close",
    "family",
    "side",
    "slug",
    "event_slug",
    "title",
    "outcome",
    "outcome_index",
    "price",
    "size",
    "notional",
    "price_band",
    "asset",
    "condition_id",
    "tx_hash",
]

MARKET_FIELDS = [
    "updated_at",
    "slug",
    "family",
    "title",
    "trades",
    "both_sides",
    "outcome_count",
    "first_trade_time",
    "last_trade_time",
    "entry_span_seconds",
    "primary_outcome",
    "primary_cost",
    "primary_shares",
    "primary_avg",
    "secondary_outcome",
    "secondary_cost",
    "secondary_shares",
    "secondary_avg",
    "matched_pairs_est",
    "pair_cost_est",
    "hedge_ratio_cost",
    "hedge_ratio_shares",
    "net_outcome",
    "net_shares_est",
    "total_buy_cost",
    "price_bands",
]

POSITION_FIELDS = [
    "seen_at",
    "slug",
    "family",
    "title",
    "outcome",
    "size",
    "avg_price",
    "cur_price",
    "current_value",
    "cash_pnl",
    "percent_pnl",
    "mergeable",
    "redeemable",
    "asset",
]

SNAPSHOT_FIELDS = [
    "seen_at",
    "timestamp",
    "family",
    "symbol",
    "underlying_price",
    "binance_rest_ms",
    "slug",
    "title",
    "condition_id",
    "gamma_ms",
    "seconds_from_open",
    "seconds_to_close",
    "up_token",
    "down_token",
    "up_buy",
    "down_buy",
    "up_sell",
    "down_sell",
    "clob_price_total_ms",
    "up_bid",
    "up_ask",
    "down_bid",
    "down_ask",
    "clob_book_total_ms",
    "up_spread",
    "down_spread",
    "up_down_buy_sum",
    "ask_pair_sum",
    "bid_pair_sum",
    "known_jet_market",
    "jet_trade_count",
    "jet_both_sides",
    "last_jet_trade_time",
    "last_jet_trade_age_s",
    "snapshot_total_ms",
    "error",
]

TRADE_CONTEXT_FIELDS = [
    "seen_at",
    "trade_time",
    "trade_timestamp",
    "trade_seen_lag_s",
    "family",
    "side",
    "slug",
    "title",
    "outcome",
    "trade_price",
    "trade_size",
    "trade_notional",
    "trade_seconds_from_open",
    "trade_seconds_to_close",
    "snapshot_seen_at",
    "snapshot_timestamp",
    "snapshot_seconds_from_open",
    "snapshot_seconds_to_close",
    "symbol",
    "underlying_price",
    "binance_rest_ms",
    "up_buy",
    "down_buy",
    "up_sell",
    "down_sell",
    "up_bid",
    "up_ask",
    "down_bid",
    "down_ask",
    "up_down_buy_sum",
    "ask_pair_sum",
    "bid_pair_sum",
    "up_spread",
    "down_spread",
    "gamma_ms",
    "clob_price_total_ms",
    "clob_book_total_ms",
    "snapshot_total_ms",
    "known_jet_market",
    "jet_trade_count",
    "jet_both_sides",
    "error",
]


def position_change_key(row: dict[str, Any]) -> str:
    return f"{row['slug']}|{row['outcome']}|{row['asset']}"


def changed_position(prev: dict[str, Any] | None, row: dict[str, Any]) -> bool:
    if prev is None:
        return True
    for key in ("size", "cur_price", "current_value", "cash_pnl", "percent_pnl"):
        if abs(float(prev.get(key, 0)) - float(row.get(key, 0))) > 0.0001:
            return True
    return False


def snapshot_slug(family: str) -> tuple[str, int, str]:
    prefix, period_seconds, symbol = SNAPSHOT_FAMILIES[family]
    start_ts = current_window_start(period_seconds)
    return f"{prefix}-{start_ts}", period_seconds, symbol


def symbol_for_market(family: str, slug: str) -> str:
    if family in SNAPSHOT_FAMILIES:
        return SNAPSHOT_FAMILIES[family][2]
    slug_lower = slug.lower()
    if slug_lower.startswith("btc-") or "btc-updown" in slug_lower:
        return "BTCUSDT"
    if slug_lower.startswith("eth-") or "eth-updown" in slug_lower:
        return "ETHUSDT"
    return ""


def active_snapshot_families(raw: str) -> list[str]:
    families = []
    for item in raw.split(","):
        family = item.strip().lower()
        if family and family in SNAPSHOT_FAMILIES and family not in families:
            families.append(family)
    return families


def build_snapshot_row(
    args,
    family: str,
    markets: dict[str, Any],
    slug_override: str = "",
) -> dict[str, Any]:
    now = time.time()
    snapshot_started = time.perf_counter()
    if slug_override:
        slug = slug_override
        symbol = symbol_for_market(family, slug)
    else:
        slug, _, symbol = snapshot_slug(family)
    row = {
        "seen_at": utc_iso(now),
        "timestamp": int(now),
        "family": family,
        "symbol": symbol,
        "underlying_price": 0.0,
        "binance_rest_ms": 0.0,
        "slug": slug,
        "title": "",
        "condition_id": "",
        "gamma_ms": 0.0,
        "seconds_from_open": round(seconds_from_open(slug, now), 3),
        "seconds_to_close": round(seconds_to_close(slug, now), 3),
        "up_token": "",
        "down_token": "",
        "up_buy": 0.0,
        "down_buy": 0.0,
        "up_sell": 0.0,
        "down_sell": 0.0,
        "clob_price_total_ms": 0.0,
        "up_bid": 0.0,
        "up_ask": 0.0,
        "down_bid": 0.0,
        "down_ask": 0.0,
        "clob_book_total_ms": 0.0,
        "up_spread": 0.0,
        "down_spread": 0.0,
        "up_down_buy_sum": 0.0,
        "ask_pair_sum": 0.0,
        "bid_pair_sum": 0.0,
        "known_jet_market": slug in markets,
        "jet_trade_count": 0,
        "jet_both_sides": False,
        "last_jet_trade_time": "",
        "last_jet_trade_age_s": 0.0,
        "snapshot_total_ms": 0.0,
        "error": "",
    }

    if slug in markets:
        market_summary = summarize_market(slug, markets[slug])
        row["jet_trade_count"] = market_summary["trades"]
        row["jet_both_sides"] = market_summary["both_sides"]
        last_ts = int(markets[slug].get("last_ts") or 0)
        if last_ts:
            row["last_jet_trade_time"] = utc_iso(last_ts)
            row["last_jet_trade_age_s"] = round(max(0.0, now - last_ts), 3)

    errors = []
    try:
        price, elapsed_ms = fetch_symbol_price_timed(symbol, args.snapshot_timeout)
        row["underlying_price"] = price
        row["binance_rest_ms"] = round(elapsed_ms, 3)
    except Exception as exc:
        errors.append(f"binance:{exc}")

    try:
        token_data = fetch_market_tokens(slug, args.snapshot_timeout)
        row["title"] = token_data.get("title", "")
        row["condition_id"] = token_data.get("condition_id", "")
        row["up_token"] = token_data.get("up_token", "")
        row["down_token"] = token_data.get("down_token", "")
        row["gamma_ms"] = round(as_float(token_data.get("gamma_ms")), 3)
        if token_data.get("error"):
            errors.append(str(token_data["error"]))
    except Exception as exc:
        errors.append(f"gamma:{exc}")

    try:
        clob_price_ms = 0.0
        row["up_buy"], elapsed_ms = fetch_clob_price_timed(
            args.clob_url, row["up_token"], "BUY", args.snapshot_timeout
        )
        clob_price_ms += elapsed_ms
        row["down_buy"], elapsed_ms = fetch_clob_price_timed(
            args.clob_url, row["down_token"], "BUY", args.snapshot_timeout
        )
        clob_price_ms += elapsed_ms
        row["up_sell"], elapsed_ms = fetch_clob_price_timed(
            args.clob_url, row["up_token"], "SELL", args.snapshot_timeout
        )
        clob_price_ms += elapsed_ms
        row["down_sell"], elapsed_ms = fetch_clob_price_timed(
            args.clob_url, row["down_token"], "SELL", args.snapshot_timeout
        )
        clob_price_ms += elapsed_ms
        row["clob_price_total_ms"] = round(clob_price_ms, 3)
    except Exception as exc:
        errors.append(f"clob_price:{exc}")

    if args.snapshot_books:
        try:
            clob_book_ms = 0.0
            row["up_bid"], row["up_ask"], elapsed_ms = best_book_prices_timed(
                args.clob_url, row["up_token"], args.snapshot_timeout
            )
            clob_book_ms += elapsed_ms
            row["down_bid"], row["down_ask"], elapsed_ms = best_book_prices_timed(
                args.clob_url, row["down_token"], args.snapshot_timeout
            )
            clob_book_ms += elapsed_ms
            row["clob_book_total_ms"] = round(clob_book_ms, 3)
        except Exception as exc:
            errors.append(f"clob_book:{exc}")

    row["up_spread"] = round(row["up_ask"] - row["up_bid"], 6) if row["up_ask"] and row["up_bid"] else 0.0
    row["down_spread"] = (
        round(row["down_ask"] - row["down_bid"], 6) if row["down_ask"] and row["down_bid"] else 0.0
    )
    row["up_down_buy_sum"] = (
        round(row["up_buy"] + row["down_buy"], 6) if row["up_buy"] and row["down_buy"] else 0.0
    )
    row["ask_pair_sum"] = (
        round(row["up_ask"] + row["down_ask"], 6) if row["up_ask"] and row["down_ask"] else 0.0
    )
    row["bid_pair_sum"] = (
        round(row["up_bid"] + row["down_bid"], 6) if row["up_bid"] and row["down_bid"] else 0.0
    )
    row["snapshot_total_ms"] = round((time.perf_counter() - snapshot_started) * 1000, 3)
    row["error"] = "; ".join(errors)
    return row


def build_trade_context_row(trade_row: dict[str, Any], snapshot_row: dict[str, Any]) -> dict[str, Any]:
    seen_ts = as_float(snapshot_row.get("timestamp"))
    trade_ts = as_float(trade_row.get("timestamp"))
    return {
        "seen_at": snapshot_row.get("seen_at", ""),
        "trade_time": trade_row.get("trade_time", ""),
        "trade_timestamp": int(trade_ts) if trade_ts else 0,
        "trade_seen_lag_s": round(max(0.0, seen_ts - trade_ts), 3) if seen_ts and trade_ts else 0.0,
        "family": trade_row.get("family", ""),
        "side": trade_row.get("side", ""),
        "slug": trade_row.get("slug", ""),
        "title": trade_row.get("title", ""),
        "outcome": trade_row.get("outcome", ""),
        "trade_price": trade_row.get("price", 0.0),
        "trade_size": trade_row.get("size", 0.0),
        "trade_notional": trade_row.get("notional", 0.0),
        "trade_seconds_from_open": trade_row.get("seconds_from_open", 0.0),
        "trade_seconds_to_close": trade_row.get("seconds_to_close", 0.0),
        "snapshot_seen_at": snapshot_row.get("seen_at", ""),
        "snapshot_timestamp": snapshot_row.get("timestamp", 0),
        "snapshot_seconds_from_open": snapshot_row.get("seconds_from_open", 0.0),
        "snapshot_seconds_to_close": snapshot_row.get("seconds_to_close", 0.0),
        "symbol": snapshot_row.get("symbol", ""),
        "underlying_price": snapshot_row.get("underlying_price", 0.0),
        "binance_rest_ms": snapshot_row.get("binance_rest_ms", 0.0),
        "up_buy": snapshot_row.get("up_buy", 0.0),
        "down_buy": snapshot_row.get("down_buy", 0.0),
        "up_sell": snapshot_row.get("up_sell", 0.0),
        "down_sell": snapshot_row.get("down_sell", 0.0),
        "up_bid": snapshot_row.get("up_bid", 0.0),
        "up_ask": snapshot_row.get("up_ask", 0.0),
        "down_bid": snapshot_row.get("down_bid", 0.0),
        "down_ask": snapshot_row.get("down_ask", 0.0),
        "up_down_buy_sum": snapshot_row.get("up_down_buy_sum", 0.0),
        "ask_pair_sum": snapshot_row.get("ask_pair_sum", 0.0),
        "bid_pair_sum": snapshot_row.get("bid_pair_sum", 0.0),
        "up_spread": snapshot_row.get("up_spread", 0.0),
        "down_spread": snapshot_row.get("down_spread", 0.0),
        "gamma_ms": snapshot_row.get("gamma_ms", 0.0),
        "clob_price_total_ms": snapshot_row.get("clob_price_total_ms", 0.0),
        "clob_book_total_ms": snapshot_row.get("clob_book_total_ms", 0.0),
        "snapshot_total_ms": snapshot_row.get("snapshot_total_ms", 0.0),
        "known_jet_market": snapshot_row.get("known_jet_market", False),
        "jet_trade_count": snapshot_row.get("jet_trade_count", 0),
        "jet_both_sides": snapshot_row.get("jet_both_sides", False),
        "error": snapshot_row.get("error", ""),
    }


def print_trade(row: dict[str, Any], market_row: dict[str, Any]):
    ttc = row["seconds_to_close"]
    if ttc > 0:
        ttc_label = f"T-{ttc:.0f}s"
    elif ttc < 0:
        ttc_label = f"T+{abs(ttc):.0f}s"
    else:
        ttc_label = "T-?"
    hedge = market_row.get("hedge_ratio_cost", 0)
    print(
        f"[{row['trade_time']}] {row['side']} {row['family']} {row['outcome']} "
        f"{row['size']:.2f} @ {row['price']:.3f} = ${row['notional']:.2f} "
        f"{ttc_label} | both={market_row['both_sides']} hedge_cost={hedge:.2f} "
        f"| {row['slug']}"
    )


def poll_once(args, state: dict[str, Any], out_dir: Path, wallet: str) -> dict[str, int]:
    seen = set(state.get("seen_trades", []))
    markets = state.setdefault("markets", {})
    trades_raw = fetch_trades(wallet, args.limit)
    new_trade_rows = []
    new_raw_rows = []
    trade_context_rows = []
    context_snapshot_cache = {}

    for trade in sorted(trades_raw, key=lambda x: float(x.get("timestamp") or 0)):
        key = trade_key(trade)
        if key in seen:
            continue
        row = normalize_trade(trade)
        update_market(markets, row)
        market_row = summarize_market(row["slug"], markets[row["slug"]])
        print_trade(row, market_row)
        seen.add(key)
        new_trade_rows.append(row)
        new_raw_rows.append({"seen_at": utc_iso(), "key": key, "raw": trade})
        if args.snapshots and row["family"] in SNAPSHOT_FAMILIES:
            context_key = row["slug"]
            if context_key not in context_snapshot_cache:
                context_snapshot_cache[context_key] = build_snapshot_row(
                    args,
                    row["family"],
                    markets,
                    slug_override=row["slug"],
                )
            trade_context_rows.append(build_trade_context_row(row, context_snapshot_cache[context_key]))

    if new_trade_rows:
        append_csv(out_dir / "trades.csv", TRADE_FIELDS, new_trade_rows)
        append_jsonl(out_dir / "trades_raw.jsonl", new_raw_rows)
        append_csv(out_dir / "trade_context.csv", TRADE_CONTEXT_FIELDS, trade_context_rows)
        state["seen_trades"] = list(seen)[-20000:]

    market_rows = [summarize_market(slug, market) for slug, market in markets.items()]
    market_rows.sort(key=lambda x: x["last_trade_time"], reverse=True)
    write_csv(out_dir / "markets_latest.csv", MARKET_FIELDS, market_rows)
    if new_trade_rows:
        append_csv(out_dir / "markets_history.csv", MARKET_FIELDS, market_rows[:50])

    snapshot_rows = []
    if args.snapshots:
        for family in active_snapshot_families(args.snapshot_families):
            snapshot_rows.append(build_snapshot_row(args, family, markets))
        append_csv(out_dir / "snapshots.csv", SNAPSHOT_FIELDS, snapshot_rows)
        write_csv(out_dir / "snapshots_latest.csv", SNAPSHOT_FIELDS, snapshot_rows)

    position_changes = []
    pos_count = 0
    if args.positions:
        positions = [normalize_position(p) for p in fetch_positions(wallet, args.position_limit)]
        pos_count = len(positions)
        write_csv(out_dir / "positions_latest.csv", POSITION_FIELDS, positions)
        prev_positions = state.setdefault("positions", {})
        for pos in positions:
            key = position_change_key(pos)
            prev = prev_positions.get(key)
            if changed_position(prev, pos):
                position_changes.append(pos)
            prev_positions[key] = pos
        append_csv(out_dir / "position_changes.csv", POSITION_FIELDS, position_changes)

    save_state(out_dir / "state.json", state)
    return {
        "new_trades": len(new_trade_rows),
        "markets": len(markets),
        "snapshots": len(snapshot_rows),
        "positions": pos_count,
        "position_changes": len(position_changes),
    }


def main():
    if load_dotenv:
        load_dotenv()

    parser = argparse.ArgumentParser(description="Follow JetFadil public Polymarket activity.")
    parser.add_argument("--username", default=os.getenv("FOLLOW_USERNAME", DEFAULT_USERNAME))
    parser.add_argument("--wallet", default=os.getenv("FOLLOW_WALLET", ""))
    parser.add_argument("--poll-seconds", type=float, default=float(os.getenv("FOLLOW_POLL_SECONDS", "15")))
    parser.add_argument("--limit", type=int, default=int(os.getenv("FOLLOW_TRADE_LIMIT", "500")))
    parser.add_argument("--position-limit", type=int, default=int(os.getenv("FOLLOW_POSITION_LIMIT", "500")))
    parser.add_argument("--log-dir", default=os.getenv("FOLLOW_LOG_DIR", "research_logs/jetfadil"))
    parser.add_argument("--clob-url", default=os.getenv("CLOB_API_URL", CLOB_API))
    parser.add_argument(
        "--snapshot-families",
        default=os.getenv("FOLLOW_SNAPSHOT_FAMILIES", "btc5m,eth5m,btc15m"),
        help="Comma-separated current markets to snapshot: btc5m,eth5m,btc15m,eth15m.",
    )
    parser.add_argument(
        "--snapshot-timeout",
        type=float,
        default=float(os.getenv("FOLLOW_SNAPSHOT_TIMEOUT", "4")),
    )
    parser.add_argument("--once", action="store_true", help="Fetch once and exit.")
    parser.add_argument("--no-positions", dest="positions", action="store_false")
    parser.add_argument("--no-snapshots", dest="snapshots", action="store_false")
    parser.add_argument("--no-snapshot-books", dest="snapshot_books", action="store_false")
    parser.set_defaults(positions=True, snapshots=True, snapshot_books=True)
    args = parser.parse_args()

    username, wallet = resolve_wallet(args.username, args.wallet)
    out_dir = Path(args.log_dir)
    state = load_state(out_dir / "state.json")
    state["username"] = username
    state["wallet"] = wallet

    print("JetFadil Follow Bot")
    print("=" * 70)
    print(f"profile={username} wallet={wallet}")
    print(f"log_dir={out_dir.resolve()}")
    print(f"poll={args.poll_seconds}s limit={args.limit} positions={args.positions}")
    print(
        f"snapshots={args.snapshots} families={active_snapshot_families(args.snapshot_families)} "
        f"books={args.snapshot_books}"
    )
    print("read-only: no wallet, no orders")
    print("=" * 70)

    while True:
        try:
            stats = poll_once(args, state, out_dir, wallet)
            print(
                f"[{utc_iso()}] new_trades={stats['new_trades']} "
                f"markets={stats['markets']} snapshots={stats['snapshots']} "
                f"positions={stats['positions']} "
                f"position_changes={stats['position_changes']}"
            )
        except KeyboardInterrupt:
            print("\nStopping follow bot.")
            break
        except Exception as exc:
            print(f"[error] {exc}")

        if args.once:
            break
        time.sleep(max(2.0, args.poll_seconds))


if __name__ == "__main__":
    main()
