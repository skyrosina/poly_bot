"""Order executor for Polymarket CLOB V2.

Polymarket production trading moved from the legacy py-clob-client package to
py-clob-client-v2. This executor keeps the bot's balance-verified trade flow,
but initializes the V2 SDK with an explicit signature type and funder address.

Current signature types:
  0 = EOA
  1 = POLY_PROXY
  2 = POLY_GNOSIS_SAFE
  3 = POLY_1271 deposit wallet

New API users should generally use signature type 3 with the deposit wallet
address as the funder. Existing proxy/Safe users can keep using 1 or 2.
"""

import time
from dataclasses import dataclass
from typing import Optional

from py_clob_client_v2 import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    MarketOrderArgs,
    OrderArgs,
    OrderPayload,
    OrderType,
    PartialCreateOrderOptions,
    Side,
    SignatureTypeV2,
)


FILLED = "FILLED"
PARTIAL = "PARTIAL"
REJECTED = "REJECTED"
FAILED = "FAILED"

MIN_SHARES = 1.0
MIN_AMOUNT_USD = 1.0
MAX_BUY_PRICE = 0.90
POLY_MIN_NOTIONAL = 5.0
DEFAULT_CLOB_API_URL = "https://clob.polymarket.com"
DEFAULT_CHAIN_ID = 137


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    status: str = FAILED
    side: str = ""
    price: float = 0.0
    amount_usd: float = 0.0
    shares: float = 0.0
    shares_remaining: float = 0.0
    token_id: str = ""
    error: str = ""
    dry_run: bool = True


def calculate_order_size(price: float, max_usd: float) -> tuple[float, float]:
    """Integer shares times cents price = clean 2-decimal collateral amount."""
    if price <= 0 or max_usd <= 0:
        return 0.0, 0.0

    price_cents = round(price * 100)
    max_usd_cents = int(max_usd * 100)
    max_shares = max_usd_cents // price_cents if price_cents > 0 else 0

    if max_shares < MIN_SHARES:
        min_cost_cents = int(MIN_SHARES) * price_cents
        if min_cost_cents <= max_usd_cents:
            max_shares = int(MIN_SHARES)
        else:
            return 0.0, 0.0

    shares = int(max_shares)
    spend = shares * price_cents / 100.0
    if shares < MIN_SHARES:
        return 0.0, 0.0
    return float(shares), spend


def _collateral_units_to_usd(raw_balance) -> float:
    """Parse CLOB collateral balance responses.

    The CLOB balance endpoint commonly returns integer base units with 6
    decimals, but this also accepts already-decimal strings for compatibility.
    """
    if raw_balance is None:
        return 0.0

    if isinstance(raw_balance, str):
        value = raw_balance.strip()
        if not value:
            return 0.0
        if value.isdigit():
            return int(value) / 1e6
        return float(value)

    if isinstance(raw_balance, int):
        return raw_balance / 1e6

    numeric = float(raw_balance)
    return numeric / 1e6 if numeric > 1_000_000 else numeric


class Executor:
    def __init__(
        self,
        private_key: str,
        funder_address: str = "",
        signature_type: int = 3,
        dry_run: bool = True,
        clob_api_url: str = DEFAULT_CLOB_API_URL,
        chain_id: int = DEFAULT_CHAIN_ID,
        clob_api_key: str = "",
        clob_api_secret: str = "",
        clob_api_passphrase: str = "",
        safe_address: str = "",
    ):
        self.dry_run = dry_run
        self.private_key = private_key.strip()
        self.funder_address = (funder_address or safe_address).strip()
        self.signature_type = int(signature_type)
        self.clob_api_url = clob_api_url.rstrip("/") or DEFAULT_CLOB_API_URL
        self.chain_id = int(chain_id)
        self.clob_api_key = clob_api_key.strip()
        self.clob_api_secret = clob_api_secret.strip()
        self.clob_api_passphrase = clob_api_passphrase.strip()
        self.client: Optional[ClobClient] = None
        self._initialized = False

    def initialize(self) -> bool:
        try:
            if not self.private_key:
                raise ValueError("PRIVATE_KEY is required for live trading")

            signature_type = SignatureTypeV2(self.signature_type)
            if signature_type != SignatureTypeV2.EOA and not self.funder_address:
                raise ValueError(
                    "FUNDER_ADDRESS is required when SIGNATURE_TYPE is 1, 2, or 3"
                )

            creds = self._configured_api_creds()
            self.client = ClobClient(
                host=self.clob_api_url,
                chain_id=self.chain_id,
                key=self.private_key,
                creds=creds,
                signature_type=signature_type,
                funder=self.funder_address or None,
                retry_on_error=True,
            )

            if creds is None:
                self.client.set_api_creds(self.client.create_or_derive_api_key())

            self._initialized = True
            self.sync_balance_allowance()

            print(f"[executor] Initialized ({'DRY RUN' if self.dry_run else 'LIVE'})")
            print(f"[executor] CLOB: {self.clob_api_url} | chain_id={self.chain_id}")
            print(f"[executor] Signature type: {int(signature_type)} ({signature_type.name})")
            print(f"[executor] Signer address: {self.client.get_address()}")
            print(f"[executor] Funder address: {self.funder_address or self.client.get_address()}")
            print(f"[executor] Max buy price: ${MAX_BUY_PRICE:.2f}")
            return True
        except Exception as e:
            print(f"[executor] Init failed: {e}")
            return False

    def _configured_api_creds(self) -> Optional[ApiCreds]:
        parts = [self.clob_api_key, self.clob_api_secret, self.clob_api_passphrase]
        if not any(parts):
            return None
        if not all(parts):
            raise ValueError(
                "Set all three CLOB_API_KEY, CLOB_SECRET, and CLOB_PASS_PHRASE, "
                "or leave all blank so the SDK can derive credentials"
            )
        return ApiCreds(
            api_key=self.clob_api_key,
            api_secret=self.clob_api_secret,
            api_passphrase=self.clob_api_passphrase,
        )

    def health_check(self) -> bool:
        if not self._initialized or not self.client:
            return False
        self.client.get_ok()
        return True

    def sync_balance_allowance(self, token_id: str = "") -> bool:
        if not self._initialized or not self.client:
            return False
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL if token_id else AssetType.COLLATERAL,
                token_id=token_id or None,
            )
            self.client.update_balance_allowance(params)
            return True
        except Exception as e:
            print(f"[executor] Balance allowance sync failed: {e}")
            return False

    def get_balance(self) -> float:
        if not self._initialized or not self.client:
            return 0.0
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = self.client.get_balance_allowance(params)
            return _collateral_units_to_usd(bal.get("balance", 0))
        except Exception as e:
            print(f"[executor] Balance check failed: {e}")
            return 0.0

    def get_market_price(self, token_id: str, side: str, amount_usd: float) -> float:
        if not self._initialized or not self.client:
            return 0.0
        try:
            price = self.client.calculate_market_price(
                token_id=token_id,
                side=side,
                amount=amount_usd,
                order_type=OrderType.FOK,
            )
            return float(price) if price else 0.0
        except Exception as e:
            err = str(e).lower()
            if "no match" not in err and "none" not in err:
                print(f"[executor] Price check failed: {e}")
            return 0.0

    def buy(self, token_id: str, amount_usd: float, price: float = 0.0) -> OrderResult:
        """Buy with a V2 limit order using integer shares and 2-decimal prices."""
        amount_usd = round(float(amount_usd), 2)
        if amount_usd < MIN_AMOUNT_USD:
            return OrderResult(
                success=False,
                status=REJECTED,
                error=f"Amount ${amount_usd:.2f} below min",
                side="BUY",
            )

        if self.dry_run:
            sim_price = 0.55
            return OrderResult(
                success=True,
                order_id=f"DRY-{int(time.time())}",
                status=FILLED,
                side="BUY",
                price=sim_price,
                amount_usd=amount_usd,
                shares=amount_usd / sim_price,
                token_id=token_id[:16] + "...",
                dry_run=True,
            )

        if not self._initialized or not self.client:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        if price > 0:
            market_price = round(price, 2)
        else:
            market_price = self.get_market_price(token_id, "BUY", amount_usd)
            if market_price <= 0:
                return OrderResult(
                    success=False,
                    status=FAILED,
                    error="Could not get market price",
                    side="BUY",
                    token_id=token_id[:16] + "...",
                )
            market_price = round(market_price, 2)

        if market_price > MAX_BUY_PRICE:
            return OrderResult(
                success=False,
                status=REJECTED,
                error=f"Price ${market_price:.3f} > cap ${MAX_BUY_PRICE:.2f}",
                side="BUY",
                price=market_price,
                token_id=token_id[:16] + "...",
            )

        shares, clean_amount = calculate_order_size(market_price, amount_usd)
        if shares < 1 or clean_amount <= 0:
            return OrderResult(
                success=False,
                status=REJECTED,
                error=(
                    f"Can't afford 1 share at ${market_price:.3f} "
                    f"within ${amount_usd:.2f}"
                ),
                side="BUY",
                price=market_price,
                token_id=token_id[:16] + "...",
            )

        if clean_amount < POLY_MIN_NOTIONAL:
            return OrderResult(
                success=False,
                status=REJECTED,
                error=f"Amount ${clean_amount:.2f} < ${POLY_MIN_NOTIONAL:.0f} min",
                side="BUY",
                price=market_price,
                token_id=token_id[:16] + "...",
            )

        print(
            f"  Market price: ${market_price:.3f}/share "
            f"-> {int(shares)} shares for ${clean_amount:.2f}"
        )

        balance_before = self.get_balance()

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=market_price,
                size=float(int(shares)),
                side=Side.BUY,
            )
            result = self.client.create_and_post_order(
                order_args=order_args,
                options=PartialCreateOrderOptions(tick_size="0.01"),
                order_type=OrderType.GTC,
            )

            error = self._extract_error(result)
            if error:
                return OrderResult(
                    success=False,
                    status=REJECTED,
                    error=error,
                    side="BUY",
                    price=market_price,
                    token_id=token_id[:16] + "...",
                )

            order_id = self._extract_order_id(result) or f"posted-{int(time.time())}"
            time.sleep(5)
            return self._verify_buy_via_balance(
                order_id, market_price, float(shares), token_id, balance_before
            )

        except Exception as e:
            time.sleep(3)
            balance_after = self.get_balance()
            spent = balance_before - balance_after if balance_before > 0 else 0

            if spent > 1.0:
                actual_shares = spent / market_price if market_price > 0 else 0
                print(f"  GHOST BUY: balance dropped ${spent:.2f} despite error")
                return OrderResult(
                    success=True,
                    order_id="ghost-buy",
                    status=FILLED,
                    side="BUY",
                    price=market_price,
                    amount_usd=spent,
                    shares=actual_shares,
                    token_id=token_id[:16] + "...",
                    dry_run=False,
                )

            return OrderResult(
                success=False,
                status=FAILED,
                error=str(e),
                side="BUY",
                price=market_price,
                token_id=token_id[:16] + "...",
            )

    def _verify_buy_via_balance(
        self,
        order_id: str,
        price: float,
        shares: float,
        token_id: str,
        balance_before: float,
    ) -> OrderResult:
        for attempt in range(3):
            balance_after = self.get_balance()
            spent = balance_before - balance_after if balance_before > 0 else 0

            if spent > 0.50:
                actual_shares = spent / price if price > 0 else shares
                suffix = f" (attempt {attempt + 1})" if attempt > 0 else ""
                print(
                    f"  Balance verified{suffix}: spent ${spent:.2f} "
                    f"(~{actual_shares:.0f} shares @ ${price:.3f})"
                )
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    status=FILLED,
                    side="BUY",
                    price=price,
                    amount_usd=spent,
                    shares=actual_shares,
                    token_id=token_id[:16] + "...",
                    dry_run=False,
                )

            if order_id and not order_id.startswith("posted-"):
                fill = self._check_order(order_id)
                if fill:
                    matched = self._extract_fill(fill, price)
                    if matched:
                        suffix = f" (attempt {attempt + 1})" if attempt > 0 else ""
                        print(
                            f"  Order API verified{suffix}: "
                            f"{matched[2]:.0f} shares @ ${matched[0]:.3f}"
                        )
                        return OrderResult(
                            success=True,
                            order_id=order_id,
                            status=FILLED,
                            side="BUY",
                            price=matched[0],
                            amount_usd=matched[1],
                            shares=matched[2],
                            token_id=token_id[:16] + "...",
                            dry_run=False,
                        )

            if attempt < 2:
                time.sleep(3)

        print("  Buy unverified after 14s - NOT cancelling")
        return OrderResult(
            success=False,
            order_id=order_id,
            status=FAILED,
            error="UNVERIFIED_BUY",
            side="BUY",
            price=price,
            amount_usd=shares * price,
            shares=shares,
            token_id=token_id[:16] + "...",
        )

    def sell(self, token_id: str, shares: float, price: float = 0.0) -> OrderResult:
        """Sell shares with a V2 FAK market order, verified by balance change."""
        sell_shares = int(shares)
        if sell_shares < 1:
            return OrderResult(
                success=False,
                status=REJECTED,
                error="Less than 1 share",
                side="SELL",
            )

        if self.dry_run:
            sim_price = price if price > 0 else 0.90
            revenue = sell_shares * sim_price
            return OrderResult(
                success=True,
                order_id=f"DRY-SELL-{int(time.time())}",
                status=FILLED,
                side="SELL",
                price=sim_price,
                amount_usd=revenue,
                shares=float(sell_shares),
                shares_remaining=0.0,
                token_id=token_id[:16] + "...",
                dry_run=True,
            )

        if not self._initialized or not self.client:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        if price <= 0:
            price = self.get_market_price(token_id, "SELL", float(sell_shares))
            if price <= 0:
                return OrderResult(
                    success=False,
                    status=FAILED,
                    error="Could not get sell price",
                    side="SELL",
                    token_id=token_id[:16] + "...",
                )

        price = round(price, 2)
        sell_amount = round(sell_shares * price, 2)
        if sell_amount < POLY_MIN_NOTIONAL:
            return OrderResult(
                success=False,
                status=REJECTED,
                error=(
                    f"Notional ${sell_amount:.2f} < ${POLY_MIN_NOTIONAL:.0f} min "
                    "hold to resolution"
                ),
                side="SELL",
                price=price,
                shares=float(sell_shares),
                shares_remaining=float(sell_shares),
                token_id=token_id[:16] + "...",
            )

        print(f"  Sell: {sell_shares} shares @ ${price:.3f} = ${sell_amount:.2f}")
        balance_before = self.get_balance()

        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=float(sell_shares),
                side=Side.SELL,
                price=price,
                order_type=OrderType.FAK,
            )
            result = self.client.create_and_post_market_order(
                order_args=order_args,
                options=PartialCreateOrderOptions(tick_size="0.01"),
                order_type=OrderType.FAK,
            )
            order_id = self._extract_order_id(result)

            time.sleep(2)
            balance_after = self.get_balance()
            received = balance_after - balance_before

            if received > 0.10:
                shares_sold = received / price if price > 0 else 0
                shares_left = max(0, sell_shares - shares_sold)
                status = FILLED if shares_left < 1 else PARTIAL
                if status == PARTIAL:
                    print(
                        f"  Partial fill: sold ~{shares_sold:.0f} of {sell_shares}, "
                        f"~{shares_left:.0f} remaining"
                    )

                return OrderResult(
                    success=True,
                    order_id=order_id or "balance-verified",
                    status=status,
                    side="SELL",
                    price=price,
                    amount_usd=received,
                    shares=shares_sold,
                    shares_remaining=shares_left,
                    token_id=token_id[:16] + "...",
                    dry_run=False,
                )

            if order_id:
                fill = self._check_order(order_id)
                if fill:
                    matched = self._extract_fill(fill, price)
                    if matched:
                        return OrderResult(
                            success=True,
                            order_id=order_id,
                            status=FILLED,
                            side="SELL",
                            price=matched[0],
                            amount_usd=matched[1],
                            shares=matched[2],
                            shares_remaining=max(0, sell_shares - matched[2]),
                            token_id=token_id[:16] + "...",
                            dry_run=False,
                        )

            error = self._extract_error(result)
            if error:
                return OrderResult(
                    success=False,
                    order_id=order_id or "",
                    status=FAILED,
                    error=error,
                    side="SELL",
                    price=price,
                    token_id=token_id[:16] + "...",
                )

            if order_id:
                self.cancel_order(order_id)
            return OrderResult(
                success=False,
                order_id=order_id or "",
                status=FAILED,
                error="Sell not verified (no balance change)",
                side="SELL",
                price=price,
                token_id=token_id[:16] + "...",
            )

        except Exception as e:
            time.sleep(1)
            balance_after = self.get_balance()
            received = balance_after - balance_before
            if received > 0.10:
                shares_sold = received / price if price > 0 else 0
                shares_left = max(0, sell_shares - shares_sold)
                print(f"  Ghost sell: got ${received:.2f} despite error")
                return OrderResult(
                    success=True,
                    order_id="ghost-sell",
                    status=PARTIAL if shares_left >= 1 else FILLED,
                    side="SELL",
                    price=price,
                    amount_usd=received,
                    shares=shares_sold,
                    shares_remaining=shares_left,
                    token_id=token_id[:16] + "...",
                    dry_run=False,
                )

            return OrderResult(
                success=False,
                status=FAILED,
                error=str(e),
                side="SELL",
                price=price,
                token_id=token_id[:16] + "...",
            )

    def _extract_fill(self, fill: dict, fallback_price: float) -> Optional[tuple]:
        if not isinstance(fill, dict):
            size_matched = float(getattr(fill, "size_matched", 0))
            fill_price = float(getattr(fill, "price", fallback_price))
            return None if size_matched <= 0 else (fill_price, size_matched * fill_price, size_matched)

        raw_size = (
            fill.get("size_matched")
            or fill.get("sizeMatched")
            or fill.get("filled_size")
            or fill.get("matched_size")
            or 0
        )
        size_matched = float(raw_size or 0)
        if size_matched <= 0:
            return None

        fill_price = float(fill.get("price") or fill.get("avg_price") or fallback_price)
        amount_usd = float(fill.get("amount_usd") or fill.get("amount") or size_matched * fill_price)
        return (fill_price, amount_usd, size_matched)

    def _check_order(self, order_id: str) -> Optional[dict]:
        if not self._initialized or not self.client:
            return None
        try:
            return self.client.get_order(order_id)
        except Exception as e:
            print(f"[executor] Order check failed: {e}")
            return None

    def _extract_order_id(self, result) -> str:
        if not isinstance(result, dict):
            return ""
        return (
            result.get("orderID")
            or result.get("order_id")
            or result.get("id")
            or result.get("hash")
            or ""
        )

    def _extract_error(self, result) -> str:
        if not isinstance(result, dict):
            return ""
        error = result.get("error") or result.get("errors")
        if error:
            return str(error)
        if result.get("success") is False:
            return str(result.get("message") or "Order rejected")
        return ""

    def cancel_order(self, order_id: str) -> bool:
        if self.dry_run or not self._initialized or not self.client:
            return True
        try:
            self.client.cancel_order(OrderPayload(orderID=order_id))
            return True
        except Exception as e:
            print(f"[executor] Cancel failed: {e}")
            return False

    def cancel_all(self) -> bool:
        if self.dry_run or not self._initialized or not self.client:
            return True
        try:
            self.client.cancel_all()
            return True
        except Exception as e:
            print(f"[executor] Cancel all failed: {e}")
            return False
