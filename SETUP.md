# PolyBot Setup Guide

This guide reflects the current Polymarket CLOB V2 setup.

## 1. Install Python Dependencies

Use Python 3.10 or newer.

```bash
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
```

On macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Key packages:

- `py-clob-client-v2` - Polymarket CLOB V2 SDK
- `python-dotenv` - reads `.env`
- `websockets` / `aiohttp` - Binance price feed
- `httpx[socks]` / `pysocks` - Tor SOCKS proxy support

## 2. Decide Your Polymarket Wallet Flow

`PRIVATE_KEY` is never a public address. It is the private key for the wallet
that signs orders.

`FUNDER_ADDRESS` is the Polymarket address that holds trading funds.

| Account type | `.env` settings |
| --- | --- |
| New Polymarket API/deposit wallet | `SIGNATURE_TYPE=3`, `FUNDER_ADDRESS=<deposit wallet>` |
| Existing proxy wallet | `SIGNATURE_TYPE=1`, `FUNDER_ADDRESS=<proxy wallet>` |
| Existing Gnosis Safe | `SIGNATURE_TYPE=2`, `FUNDER_ADDRESS=<Safe address>` |
| Direct EOA | `SIGNATURE_TYPE=0`, `FUNDER_ADDRESS=` can be blank |

For new API users, Polymarket's current docs recommend the deposit wallet flow:

- The deposit wallet is the funder.
- Collateral is held as pUSD/CLOB collateral in the deposit wallet.
- CLOB orders use `POLY_1271`, which is `SIGNATURE_TYPE=3`.
- Approvals must come from the deposit wallet, not the owner EOA.

Existing proxy and Safe accounts can continue using their existing funder
addresses and signature types.

## 3. Configure `.env`

Start with dry run:

```env
PRIVATE_KEY=
SIGNATURE_TYPE=3
FUNDER_ADDRESS=

CLOB_API_KEY=
CLOB_SECRET=
CLOB_PASS_PHRASE=

CLOB_API_URL=https://clob.polymarket.com
CHAIN_ID=137
DRY_RUN=true
PAPER_USE_LIVE_CLOB=true
CHECK_GEOBLOCK=true
USE_TOR=false
```

`PAPER_USE_LIVE_CLOB=true` keeps dry-run wallet-free and order-free, but uses
live Polymarket CLOB public prices for entries and position monitoring. This is
recommended because the old simulated dry-run price engine can look much better
than real execution.

If you already have CLOB API credentials, fill all three:

```env
CLOB_API_KEY=...
CLOB_SECRET=...
CLOB_PASS_PHRASE=...
```

If you leave them blank, the SDK will try to create or derive credentials from
`PRIVATE_KEY` at startup.

The bot also accepts these compatibility aliases:

```env
DEPOSIT_WALLET_ADDRESS=...
SAFE_ADDRESS=...
```

Priority is:

```text
FUNDER_ADDRESS -> DEPOSIT_WALLET_ADDRESS -> SAFE_ADDRESS
```

## 4. Strategy and Risk Settings

```env
LIVE_SAFE_MODE=true
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
VOL_FLOOR=0.12
PRICE_REFRESH_SECONDS=1.0
MAX_BTC_FEED_LAG_MS=700
```

Set `BANKROLL` to the amount you want the bot to size against. In live mode,
the bot overwrites this with the actual CLOB collateral balance after startup.

`LIVE_SAFE_MODE=true` is recommended for live testing. It forces a conservative
minimum threshold even if older `.env` values are still present.
If Binance WebSocket messages arrive more than `MAX_BTC_FEED_LAG_MS` late,
live safe mode skips new entries until the feed recovers.

## 5. Optional Telegram Alerts

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

The bot works without Telegram, but alerts are useful for live sessions.

## 6. Dry Run First

Run the setup checker before starting the bot:

```bash
py health_check.py --public-only
```

After filling `PRIVATE_KEY` and `FUNDER_ADDRESS`, run:

```bash
py health_check.py
```

It does not place orders. It checks geoblock status, CLOB auth, signer/funder
configuration, and collateral balance/allowance.

Check server/API latency:

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

The latency checker is read-only. It tests Polymarket CLOB/Gamma public
endpoints and Binance REST/WebSocket. For this bot, CLOB price/book and Binance
WebSocket should ideally average below about 350 ms.
`binance.server_time_offset` should ideally stay near zero; values under about
100-200 ms are usually fine for this bot.

If `data-stream.binance.vision` is faster on your server, set this in `.env`:

```env
BINANCE_WS_URL=wss://data-stream.binance.vision/ws/btcusdt@trade
```

The comparison also tests Binance's alternative `:443` port and `aggTrade`
streams. If one of those has clearly lower sustained lag, use that full URL as
`BINANCE_WS_URL`.

```bash
py bot.py
```

Dry run does not initialize a wallet or place orders. It checks the Binance
feed, market discovery, strategy logic, and simulated trade flow.

Run at least a few windows before switching to live.

## 7. Research: Follow JetFadil

This read-only script records JetFadil's public trades, current positions, and
per-market two-sided inventory summaries. It does not use a wallet and does not
place orders.

Run once:

```bash
python follow_jetfadil.py --once --limit 500
```

Run continuously:

```bash
python follow_jetfadil.py --poll-seconds 15 --limit 500
```

For denser market snapshots, use a shorter poll:

```bash
python follow_jetfadil.py --poll-seconds 5 --limit 500
```

Logs are written to:

```text
research_logs/jetfadil/
```

Important files:

```text
trades.csv              realized public trades
markets_latest.csv      per-market hedge/ladder summary
positions_latest.csv    current public positions
position_changes.csv    position/PnL changes
snapshots.csv           BTC/ETH price + Polymarket UP/DOWN price/book snapshots
snapshots_latest.csv    latest snapshot row per watched family
trade_context.csv       immediate snapshot captured when a new JetFadil trade appears
```

## 8. Live Checklist

Before setting `DRY_RUN=false`:

- `PRIVATE_KEY` belongs to the intended signer.
- `SIGNATURE_TYPE` matches the account flow.
- `FUNDER_ADDRESS` is the address that actually holds Polymarket funds.
- Deposit-wallet users have funded the deposit wallet, not only the owner EOA.
- Deposit-wallet users have completed required approvals/onboarding.
- CLOB API credentials are either blank or all three are set.
- `DAILY_LOSS_LIMIT` is acceptable.
- `py health_check.py` passes.

Then:

```env
DRY_RUN=false
```

```bash
py bot.py
```

## Troubleshooting

### Init fails: missing funder

If `SIGNATURE_TYPE` is `1`, `2`, or `3`, set `FUNDER_ADDRESS`.

### Invalid signature

Usually one of these is wrong:

- `SIGNATURE_TYPE`
- `FUNDER_ADDRESS`
- private key does not own/control the selected wallet flow
- deposit wallet not onboarded/approved

For deposit wallets, the order maker and signer must resolve to the deposit
wallet under `POLY_1271`.

### Not enough balance / allowance

For deposit wallets, pUSD must be in the deposit wallet. pUSD on the owner EOA
does not fund deposit-wallet CLOB orders. After funding or approvals, sync CLOB
balance/allowance; the bot does this at startup, but onboarding must be correct.

### Amsterdam / Netherlands VPS

Polymarket's official geoblock docs list `NL` / Netherlands as blocked for
order placement. An Amsterdam Droplet should be expected to fail the geoblock
check. Use a compliant eligible hosting location instead of trying to bypass
geographic restrictions.

### No trades firing

That can be normal. The strategy requires a large enough BTC move and a market
price that still leaves edge after safety filters.

## Security

Never commit `.env`. Never paste your private key into chat. Anyone with the
private key can control the wallet.
