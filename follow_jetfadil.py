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
DEFAULT_USERNAME = "jetfadil"
DEFAULT_WALLET = "0xe0229e10a858860218b6132f4234602c47bd6603"


def utc_iso(ts: float | int | None = None) -> str:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def http_json(url: str, params: dict[str, Any] | None = None, timeout: float = 10) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "PolyBot-Research/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {}


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


def position_change_key(row: dict[str, Any]) -> str:
    return f"{row['slug']}|{row['outcome']}|{row['asset']}"


def changed_position(prev: dict[str, Any] | None, row: dict[str, Any]) -> bool:
    if prev is None:
        return True
    for key in ("size", "cur_price", "current_value", "cash_pnl", "percent_pnl"):
        if abs(float(prev.get(key, 0)) - float(row.get(key, 0))) > 0.0001:
            return True
    return False


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

    if new_trade_rows:
        append_csv(out_dir / "trades.csv", TRADE_FIELDS, new_trade_rows)
        append_jsonl(out_dir / "trades_raw.jsonl", new_raw_rows)
        state["seen_trades"] = list(seen)[-20000:]

    market_rows = [summarize_market(slug, market) for slug, market in markets.items()]
    market_rows.sort(key=lambda x: x["last_trade_time"], reverse=True)
    write_csv(out_dir / "markets_latest.csv", MARKET_FIELDS, market_rows)
    if new_trade_rows:
        append_csv(out_dir / "markets_history.csv", MARKET_FIELDS, market_rows[:50])

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
    parser.add_argument("--once", action="store_true", help="Fetch once and exit.")
    parser.add_argument("--no-positions", dest="positions", action="store_false")
    parser.set_defaults(positions=True)
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
    print("read-only: no wallet, no orders")
    print("=" * 70)

    while True:
        try:
            stats = poll_once(args, state, out_dir, wallet)
            print(
                f"[{utc_iso()}] new_trades={stats['new_trades']} "
                f"markets={stats['markets']} positions={stats['positions']} "
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
