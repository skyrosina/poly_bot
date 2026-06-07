#!/usr/bin/env python3
"""Quick diagnostic: print both UP and DOWN order books for the current 5-min market."""

import os
from dotenv import load_dotenv
from market import get_current_market
from py_clob_client_v2 import ApiCreds, ClobClient, SignatureTypeV2

load_dotenv()

funder = (
    os.getenv("FUNDER_ADDRESS", "")
    or os.getenv("DEPOSIT_WALLET_ADDRESS", "")
    or os.getenv("SAFE_ADDRESS", "")
)
signature_type = SignatureTypeV2(int(os.getenv("SIGNATURE_TYPE", "3")))
creds = None
if os.getenv("CLOB_API_KEY") or os.getenv("CLOB_SECRET") or os.getenv("CLOB_PASS_PHRASE"):
    creds = ApiCreds(
        api_key=os.getenv("CLOB_API_KEY", ""),
        api_secret=os.getenv("CLOB_SECRET", ""),
        api_passphrase=os.getenv("CLOB_PASS_PHRASE", ""),
    )

# Init client
client = ClobClient(
    host=os.getenv("CLOB_API_URL", "https://clob.polymarket.com"),
    chain_id=int(os.getenv("CHAIN_ID", "137")),
    key=os.getenv("PRIVATE_KEY", ""),
    creds=creds,
    funder=funder or None,
    signature_type=signature_type,
)
if creds is None:
    client.set_api_creds(client.create_or_derive_api_key())

# Get current market
market = get_current_market(5)
if not market:
    print("No active market found")
    exit()

print(f"Market: {market}")
print(f"\nUP token:   {market.token_id_up}")
print(f"DOWN token: {market.token_id_down}")
print(f"UP price:   ${market.up_price:.3f}")
print(f"DOWN price: ${market.down_price:.3f}")


def print_book(label, token_id):
    print(f"\n{'=' * 50}")
    print(f"  {label} — {token_id[:20]}...")
    print(f"{'=' * 50}")

    book = client.get_order_book(token_id)

    # Handle both dict and object
    if hasattr(book, 'asks'):
        asks = book.asks or []
        bids = book.bids or []
    else:
        asks = book.get("asks", [])
        bids = book.get("bids", [])

    print(f"\n  ASKS (selling at):")
    if not asks:
        print(f"    (empty)")
    for level in asks[:6]:
        p = float(level.price if hasattr(level, 'price') else level.get("price", 0))
        s = float(level.size if hasattr(level, 'size') else level.get("size", 0))
        print(f"    ${p:.2f}  |  {s:.1f} shares  |  ${p * s:.2f}")

    print(f"\n  BIDS (buying at):")
    if not bids:
        print(f"    (empty)")
    for level in bids[:6]:
        p = float(level.price if hasattr(level, 'price') else level.get("price", 0))
        s = float(level.size if hasattr(level, 'size') else level.get("size", 0))
        print(f"    ${p:.2f}  |  {s:.1f} shares  |  ${p * s:.2f}")

    total_ask = sum(
        float(l.size if hasattr(l, 'size') else l.get("size", 0)) for l in asks
    )
    total_bid = sum(
        float(l.size if hasattr(l, 'size') else l.get("size", 0)) for l in bids
    )
    print(f"\n  Total ask depth: {total_ask:.0f} shares")
    print(f"  Total bid depth: {total_bid:.0f} shares")


print_book("TRADE UP (UP token book)", market.token_id_up)
print_book("TRADE DOWN (DOWN token book)", market.token_id_down)
