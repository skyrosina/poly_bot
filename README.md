# PolyBot - Polymarket BTC Up/Down Bot

PolyBot is a local Python trading bot for Polymarket BTC 5-minute Up/Down
markets. It watches Binance BTC trades in real time, estimates the probability
that BTC resolves Up or Down, and places Polymarket CLOB orders when the market
price appears stale.

This project is now wired for Polymarket CLOB V2.

## Current Polymarket API Surface

- SDK package: `py-clob-client-v2`
- Import package: `py_clob_client_v2`
- CLOB host: `https://clob.polymarket.com`
- Chain ID: `137`
- New-user wallet flow: `SIGNATURE_TYPE=3` (`POLY_1271`)
- New-user funder: Polymarket deposit wallet address
- Collateral: pUSD / CLOB collateral balance

Existing accounts can still use older account flows:

| Signature type | Meaning | Funder address |
| --- | --- | --- |
| `0` | Direct EOA | signer wallet, or blank to let SDK default |
| `1` | Existing Polymarket proxy wallet | proxy wallet address |
| `2` | Existing Gnosis Safe wallet | Safe address |
| `3` | Deposit wallet / `POLY_1271` | deposit wallet address |

The bot reads `FUNDER_ADDRESS` first, then `DEPOSIT_WALLET_ADDRESS`, then the
legacy `SAFE_ADDRESS` alias.

## Architecture

```text
bot.py                 Main loop, position lifecycle, circuit breakers
strategy.py            Probability model and Kelly sizing
executor.py            Polymarket CLOB V2 execution, balance verification
market.py              Gamma API market discovery
price_feed.py          Binance BTC price feed
tracker.py             signals/trades/executions CSV logs
telegram_notifier.py   Optional Telegram alerts
proxy.py               Optional Tor routing for CLOB traffic
```

## Quick Start

```bash
pip install -r requirements.txt
copy .env.example .env  # if you create one
```

Or edit the existing `.env` file directly. Keep `DRY_RUN=true` until the bot
connects cleanly and you have verified the wallet/funder setup.

```bash
py bot.py       # Windows
python bot.py   # macOS/Linux
```

Before live mode, run:

```bash
py health_check.py
```

This checks geoblock status, CLOB V2 auth, signer/funder configuration, and
collateral balance/allowance without placing orders.

To check route latency from the current server:

```bash
py latency_check.py --samples 5
```

On Linux/Droplet:

```bash
python latency_check.py --samples 5
```

Compare Binance market-data routes:

```bash
python latency_check.py --samples 5 --compare-binance-ws
```

Measure sustained WebSocket lag using one open connection:

```bash
python latency_check.py --samples 3 --compare-binance-ws --ws-duration 20
```

For this strategy, Binance WebSocket and Polymarket CLOB price/book checks
should ideally stay below about 350 ms average. If they are consistently slow,
move the bot closer to the faster route or widen safety filters.
`binance.server_time_offset` should ideally stay near zero; values under about
100-200 ms are usually fine for this bot.

If `data-stream.binance.vision` is faster on your server, set:

```env
BINANCE_WS_URL=wss://data-stream.binance.vision/ws/btcusdt@trade
```

The comparison also tests Binance's alternative `:443` port and `aggTrade`
streams. If one of those has clearly lower sustained lag, use that full URL as
`BINANCE_WS_URL`.

## Required `.env` Values

```env
PRIVATE_KEY=0x...
SIGNATURE_TYPE=3
FUNDER_ADDRESS=0x...

CLOB_API_URL=https://clob.polymarket.com
CHAIN_ID=137
DRY_RUN=true
PAPER_USE_LIVE_CLOB=true
CHECK_GEOBLOCK=true
USE_TOR=false
```

With `DRY_RUN=true` and `PAPER_USE_LIVE_CLOB=true`, the bot still places no
orders and does not need a wallet, but it uses live Polymarket CLOB public
prices for entries and position monitoring. This is much closer to live mode
than the old simulated dry-run price engine.

Optional API credentials:

```env
CLOB_API_KEY=
CLOB_SECRET=
CLOB_PASS_PHRASE=
```

Leave all three blank if you want the SDK to create or derive credentials from
`PRIVATE_KEY` on startup. If you provide one, provide all three.

## Strategy Defaults

```env
MIN_EDGE=0.10
MIN_PROB=0.90
SAFETY_FACTOR=0.85
MIN_BTC_DELTA=0.10
ENTRY_WINDOW_START=25
ENTRY_WINDOW_END=10
ENTRY_CONFIRM_SECONDS=2.0
KELLY_FRACTION=0.10
MIN_BET=5.0
MAX_BET=5.0
BANKROLL=100.0
DAILY_LOSS_LIMIT=5.0
PRICE_REFRESH_SECONDS=1.0
MAX_BTC_FEED_LAG_MS=700
```

Live mode defaults to `LIVE_SAFE_MODE=true`, which clamps aggressive `.env`
values to a more selective profile: `MIN_PROB>=0.90`, `MIN_EDGE>=0.10`,
`MIN_BTC_DELTA>=0.10`, `ENTRY_WINDOW_START<=25`, `ENTRY_WINDOW_END>=10`,
`ENTRY_CONFIRM_SECONDS>=2`, `KELLY_FRACTION<=0.10`, `MAX_BET<=5`,
`DAILY_LOSS_LIMIT<=5`, `VOL_FLOOR>=0.12`, and `PRICE_REFRESH_SECONDS<=1`.
If Binance WebSocket messages arrive more than `MAX_BTC_FEED_LAG_MS` late,
live safe mode skips new entries until the feed recovers.
Disable this only after you have enough live data to justify the extra risk.

The strategy holds positions to resolution. There are no stop-loss or
take-profit exits because prior live data showed they were harmful on 5-minute
markets.

## Safety Systems

- CLOB health check before live entries.
- Geoblock preflight before live mode.
- Daily loss limit.
- Balance-verified buys and sells.
- Pending-buy reconciliation at the next market window.
- Window-boundary balance sync against live CLOB collateral balance.
- Minimum notional guard before sells.

## Research: Follow JetFadil

`follow_jetfadil.py` is a read-only public-data follower. It does not use a
wallet and does not place orders. It records JetFadil's public trades,
positions, and per-market two-sided inventory summaries.

Run once:

```bash
python follow_jetfadil.py --once --limit 500
```

Run continuously:

```bash
python follow_jetfadil.py --poll-seconds 15 --limit 500
```

Outputs go to `research_logs/jetfadil/`:

```text
trades.csv
markets_latest.csv
markets_history.csv
positions_latest.csv
position_changes.csv
snapshots.csv
snapshots_latest.csv
trade_context.csv
trades_raw.jsonl
state.json
```

`snapshots.csv` records the current BTC/ETH market screen at each poll: Binance
BTC/ETH price, Polymarket UP/DOWN buy prices, best bid/ask, pair sums, spread,
time to close, endpoint timings, and whether JetFadil has traded that market.
`trade_context.csv` records an immediate market snapshot whenever a new public
JetFadil trade is detected, including `trade_seen_lag_s`, so timing drift is
visible during analysis.

## Current V2 Execution Notes

- Buys use V2 `create_and_post_order` with integer shares and 2-decimal prices.
- Sells use V2 `create_and_post_market_order` with `OrderType.FAK`.
- For V2 market sells, `amount` is the number of shares to sell, not USD
  notional.
- For deposit wallets, orders must use `SIGNATURE_TYPE=3` and the deposit
  wallet as the funder.
- Funding/approvals for deposit wallets must be done from the deposit wallet,
  usually through Polymarket's relayer/onboarding flow.
- Official geoblock docs list the Netherlands (`NL`) as blocked for order
  placement, so Amsterdam VPS routes should fail preflight.

## Risk Warning

This bot can trade real money. BTC 5-minute binary markets are volatile and
all-or-nothing. Start in `DRY_RUN=true`, then test live with a small funded
wallet only after credentials, balance, market discovery, and order posting are
confirmed.
