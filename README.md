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

## Required `.env` Values

```env
PRIVATE_KEY=0x...
SIGNATURE_TYPE=3
FUNDER_ADDRESS=0x...

CLOB_API_URL=https://clob.polymarket.com
CHAIN_ID=137
DRY_RUN=true
CHECK_GEOBLOCK=true
USE_TOR=false
```

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
PRICE_REFRESH_SECONDS=2.0
```

Live mode defaults to `LIVE_SAFE_MODE=true`, which clamps aggressive `.env`
values to a more selective profile: `MIN_PROB>=0.90`, `MIN_EDGE>=0.10`,
`MIN_BTC_DELTA>=0.10`, `ENTRY_WINDOW_START<=25`, `ENTRY_WINDOW_END>=10`,
`ENTRY_CONFIRM_SECONDS>=2`, `KELLY_FRACTION<=0.10`, `MAX_BET<=5`,
`DAILY_LOSS_LIMIT<=5`, `VOL_FLOOR>=0.12`, and `PRICE_REFRESH_SECONDS<=2`.
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
