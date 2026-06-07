# AGENTS.md - PolyBot Project Context

## What This Is

PolyBot is a local Python trading bot for Polymarket BTC 5-minute Up/Down
binary markets. It watches Binance BTC prices, estimates the true Up/Down
probability, and trades when Polymarket CLOB prices appear stale.

This repo is now updated for Polymarket CLOB V2.

## Owner

JLow (`jlowplayground` on Polymarket). Solo developer. Treat this as a serious
trading operation because live mode can place real-money orders.

## Main Files

```text
bot.py                 Main loop, position lifecycle, circuit breakers
strategy.py            Brownian probability model and Kelly sizing
executor.py            Polymarket CLOB V2 execution and balance verification
market.py              Gamma API market discovery
price_feed.py          Binance real-time BTC feed
tracker.py             signals/trades/executions CSV logger
telegram_notifier.py   Telegram alerts and summaries
proxy.py               Tor proxy helper for CLOB requests
health_check.py        No-order setup/geoblock/balance preflight
```

## Current Polymarket API Facts

- Legacy `py-clob-client` is V1-era and should not be used for production CLOB
  V2 trading.
- Current dependency: `py-clob-client-v2>=1.0.1`.
- Import package: `py_clob_client_v2`.
- CLOB host: `https://clob.polymarket.com`.
- Chain ID: `137`.
- Live mode checks Polymarket geoblock before trading when
  `CHECK_GEOBLOCK=true`.
- `USE_TOR=false` by default; do not use proxying to bypass geographic
  restrictions.
- New API users should use deposit wallets with `SIGNATURE_TYPE=3`
  (`POLY_1271`) and the deposit wallet as the funder.
- Existing users may still use `SIGNATURE_TYPE=1` for proxy wallets or
  `SIGNATURE_TYPE=2` for Gnosis Safe wallets.
- Direct EOA is `SIGNATURE_TYPE=0`.
- CLOB collateral is pUSD/CLOB collateral. Balance responses are still handled
  as 6-decimal base units when returned as integer strings.

## Wallet Environment Variables

```env
PRIVATE_KEY=0x...
SIGNATURE_TYPE=3
FUNDER_ADDRESS=0x...

CLOB_API_KEY=
CLOB_SECRET=
CLOB_PASS_PHRASE=

CLOB_API_URL=https://clob.polymarket.com
CHAIN_ID=137
DRY_RUN=true
CHECK_GEOBLOCK=true
USE_TOR=false
```

Compatibility aliases:

```env
DEPOSIT_WALLET_ADDRESS=0x...
SAFE_ADDRESS=0x...
```

The bot uses `FUNDER_ADDRESS`, then `DEPOSIT_WALLET_ADDRESS`, then
`SAFE_ADDRESS`.

Never ask the user to paste a private key into chat.

## Strategy

Entry pipeline:

1. Brownian model estimates Up probability from BTC delta and seconds remaining.
2. Minimum probability gate defaults to `MIN_PROB=0.80`.
3. Minimum edge gate defaults to `MIN_EDGE=0.05`.
4. Position size uses quarter-Kelly with `$5 <= bet <= $25` defaults.

The bot holds to resolution. Prior testing showed stop exits damaged returns on
5-minute windows.

## Execution Details

- Buys use V2 `create_and_post_order` with explicit integer shares and
  2-decimal prices.
- Sells use V2 `create_and_post_market_order` with `OrderType.FAK`.
- In V2 market sells, `MarketOrderArgs.amount` is the number of shares to sell,
  not USD notional.
- All fills are verified primarily by collateral balance changes.
- Unverified buys are never cancelled automatically; the bot reconciles them at
  the next window boundary.

## Safety Systems

- CLOB health check before live entries.
- Three consecutive health/order failures halt new trades.
- Daily loss limit.
- Balance-verified buys and sells.
- Pending-buy safety net.
- Window-boundary balance sync.
- Minimum notional guard before sells.
- Official geoblock docs list `NL` / Netherlands as blocked for order
  placement; Amsterdam Droplets should fail preflight.

## Testing

No formal automated test suite exists yet. Use:

```bash
py -c "import ast, pathlib; [ast.parse(p.read_text(encoding='utf-8')) for p in pathlib.Path('.').glob('*.py')]; print('syntax ok')"
```

Then dry run:

```bash
py health_check.py
py health_check.py --public-only
DRY_RUN=true
py bot.py
```

For live validation, use a small funded wallet and compare `logs/trades.csv`
against the Polymarket CSV export.
