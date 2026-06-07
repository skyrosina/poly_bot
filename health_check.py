#!/usr/bin/env python3
"""Polymarket CLOB V2 preflight checks.

This script does not place orders. It checks:
  - .env wallet/funder settings
  - Polymarket geoblock result for the current IP
  - CLOB V2 SDK import
  - signer address derivation from PRIVATE_KEY
  - API credential create/derive, unless all CLOB creds are provided
  - collateral balance/allowance read
"""

import argparse
import json
import os
import sys
import urllib.request

from dotenv import load_dotenv


SIGNATURE_LABELS = {
    0: "EOA",
    1: "POLY_PROXY",
    2: "POLY_GNOSIS_SAFE",
    3: "POLY_1271 / deposit wallet",
}


def env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def mask(value: str, keep: int = 6) -> str:
    if not value:
        return "(blank)"
    if len(value) <= keep * 2:
        return value
    return f"{value[:keep]}...{value[-keep:]}"


def collateral_units_to_usd(raw_balance) -> float:
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


def check_geoblock(timeout: int = 10) -> dict:
    req = urllib.request.Request(
        "https://polymarket.com/api/geoblock",
        headers={"User-Agent": "PolyBot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Polymarket CLOB V2 setup")
    parser.add_argument(
        "--public-only",
        action="store_true",
        help="Only check geoblock and public CLOB health; do not use PRIVATE_KEY",
    )
    args = parser.parse_args()

    load_dotenv()

    private_key = os.getenv("PRIVATE_KEY", "").strip()
    signature_type = int(os.getenv("SIGNATURE_TYPE", "3"))
    funder = (
        os.getenv("FUNDER_ADDRESS", "").strip()
        or os.getenv("DEPOSIT_WALLET_ADDRESS", "").strip()
        or os.getenv("SAFE_ADDRESS", "").strip()
    )
    clob_api_url = os.getenv("CLOB_API_URL", "https://clob.polymarket.com").rstrip("/")
    chain_id = int(os.getenv("CHAIN_ID", "137"))
    dry_run = env_bool("DRY_RUN", "true")

    print("PolyBot CLOB V2 Health Check")
    print("=" * 34)
    print(f"DRY_RUN: {dry_run}")
    print(f"CLOB_API_URL: {clob_api_url}")
    print(f"CHAIN_ID: {chain_id}")
    print(f"SIGNATURE_TYPE: {signature_type} ({SIGNATURE_LABELS.get(signature_type, 'unknown')})")
    print(f"FUNDER_ADDRESS: {mask(funder)}")
    print(f"PRIVATE_KEY: {'set' if private_key else 'blank'}")

    if signature_type not in SIGNATURE_LABELS:
        print("\nFAIL: SIGNATURE_TYPE must be 0, 1, 2, or 3.")
        return 1

    if signature_type != 0 and not funder:
        print("\nFAIL: FUNDER_ADDRESS is required for signature types 1, 2, and 3.")
        return 1

    print("\nGeoblock")
    try:
        geo = check_geoblock()
        print(
            f"country={geo.get('country')} region={geo.get('region') or '-'} "
            f"blocked={geo.get('blocked')}"
        )
        if geo.get("blocked"):
            print("FAIL: Current IP is blocked for Polymarket order placement.")
            return 1
    except Exception as e:
        print(f"FAIL: Could not verify geoblock status: {e}")
        return 1

    print("\nCLOB SDK")
    try:
        from py_clob_client_v2 import (
            ApiCreds,
            AssetType,
            BalanceAllowanceParams,
            ClobClient,
            SignatureTypeV2,
        )
    except Exception as e:
        print(f"FAIL: py-clob-client-v2 import failed: {e}")
        print("Install dependencies first: py -m pip install -r requirements.txt")
        return 1

    try:
        public_client = ClobClient(host=clob_api_url, chain_id=chain_id)
        public_client.get_ok()
        print("public CLOB health: ok")
    except Exception as e:
        print(f"FAIL: public CLOB health check failed: {e}")
        return 1

    if args.public_only:
        print("\nOK: public checks passed.")
        return 0

    if not private_key:
        print("\nFAIL: PRIVATE_KEY is blank; authenticated checks cannot run.")
        return 1

    key = os.getenv("CLOB_API_KEY", "").strip()
    secret = os.getenv("CLOB_SECRET", "").strip()
    passphrase = os.getenv("CLOB_PASS_PHRASE", "").strip()
    creds_parts = [key, secret, passphrase]
    if any(creds_parts) and not all(creds_parts):
        print("\nFAIL: CLOB_API_KEY, CLOB_SECRET, and CLOB_PASS_PHRASE must be set together.")
        return 1

    creds = (
        ApiCreds(api_key=key, api_secret=secret, api_passphrase=passphrase)
        if all(creds_parts)
        else None
    )

    print("\nAuthenticated CLOB")
    try:
        client = ClobClient(
            host=clob_api_url,
            chain_id=chain_id,
            key=private_key,
            creds=creds,
            signature_type=SignatureTypeV2(signature_type),
            funder=funder or None,
        )
        print(f"signer address: {mask(client.get_address())}")

        if creds is None:
            print("api credentials: creating/deriving via SDK")
            client.set_api_creds(client.create_or_derive_api_key())
        else:
            print("api credentials: loaded from .env")

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        try:
            client.update_balance_allowance(params)
        except Exception as e:
            print(f"balance allowance sync warning: {e}")

        bal = client.get_balance_allowance(params)
        balance = collateral_units_to_usd(bal.get("balance", 0))
        allowance = collateral_units_to_usd(bal.get("allowance", 0))
        print(f"collateral balance: ${balance:.2f}")
        print(f"collateral allowance: ${allowance:.2f}")

        if balance <= 0:
            print("WARN: balance is zero; funder/deposit wallet may not be funded or synced.")
        if allowance <= 0:
            print("WARN: allowance is zero; approvals may not be complete.")

    except Exception as e:
        print(f"FAIL: authenticated CLOB check failed: {e}")
        return 1

    print("\nOK: health check completed. No orders were placed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
