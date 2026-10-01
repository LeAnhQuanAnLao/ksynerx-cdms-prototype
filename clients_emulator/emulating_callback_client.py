"""Client simulating external systems pushing CDC webhook callbacks to CDMS."""

import argparse
from datetime import datetime, timezone, timedelta
import random
import time
from typing import Any, Dict, List
import httpx
from faker import Faker

fake = Faker(["vi_VN", "en_US"])

WAREHOUSES = ["WH-HN-01", "WH-HCM-01", "WH-DN-01"]
PARTNER_SKUS = [f"PARTNER-ELEC-{i}" for i in range(101, 115)]


def generate_mock_event(
    partner_sku: str,
    warehouse: str,
    timestamp: datetime,
    qty_delta: int = 0,
) -> Dict[str, Any]:
    """Generate a realistic inventory event."""
    physical = 100 + qty_delta
    avail = max(0, physical - 10)
    return {
        "warehouseCode": warehouse,
        "partnerSKU": partner_sku,
        "sku": f"SKU-{partner_sku.split('-')[-1]}",
        "productName": f"Smart Device {partner_sku}",
        "unitCode": "CAI",
        "conditionTypeCode": "NEW",
        "physicalQty": physical,
        "availableQty": avail,
        "pendingInQty": 5,
        "pendingOutQty": 10,
        "freezeQty": 0,
        "inTransitQty": 0,
        "isActive": True,
        "lastUpdatedDate": timestamp.isoformat(),
    }


import hashlib
import hmac
import json
from typing import Any, Dict, List, Optional


def push_webhook(
    url: str,
    payload: Dict[str, Any],
    api_key: Optional[str] = None,
    webhook_secret: Optional[str] = None,
) -> Dict[str, Any]:
    """Send HTTP POST request to CDMS webhook endpoint with optional security headers."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    body_bytes = json.dumps(payload).encode("utf-8")
    if webhook_secret:
        ts = str(int(time.time()))
        sig = hmac.new(webhook_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        headers["X-CDMS-Signature"] = sig
        headers["X-CDMS-Timestamp"] = ts

    with httpx.Client(timeout=10.0) as client:
        resp = client.post(url, content=body_bytes, headers=headers)
        resp.raise_for_status()
        return resp.json()


def run_scenario(
    cdms_url: str,
    scenario: str,
    api_key: Optional[str] = None,
    secret: Optional[str] = None,
) -> None:
    """Execute demonstration scenarios."""
    endpoint = f"{cdms_url.rstrip('/')}/api/v1/cdc/webhook"
    now = datetime.now(timezone.utc)

    if scenario == "normal":
        print("[Scenario: Normal] Pushing 3 new inventory events...")
        items = [
            generate_mock_event(PARTNER_SKUS[0], WAREHOUSES[0], now),
            generate_mock_event(PARTNER_SKUS[1], WAREHOUSES[0], now),
            generate_mock_event(PARTNER_SKUS[2], WAREHOUSES[1], now),
        ]
        res = push_webhook(endpoint, {"items": items, "source": "WEBHOOK_CLIENT"}, api_key=api_key, webhook_secret=secret)
        print(f"Result: Recorded={res['recorded_changes']}, Duplicates={res['ignored_duplicates']}")

    elif scenario == "duplicate":
        print("[Scenario: Duplicate] Pushing exact same payload twice...")
        items = [generate_mock_event(PARTNER_SKUS[3], WAREHOUSES[0], now)]
        payload = {"event_id": "TEST-DUP-001", "items": items, "source": "WEBHOOK_CLIENT"}
        res1 = push_webhook(endpoint, payload, api_key=api_key, webhook_secret=secret)
        print(f"First push: Recorded={res1['recorded_changes']}, Duplicates={res1['ignored_duplicates']}")
        res2 = push_webhook(endpoint, payload, api_key=api_key, webhook_secret=secret)
        print(f"Second push (duplicate): Recorded={res2['recorded_changes']}, Duplicates={res2['ignored_duplicates']}")

    elif scenario == "outdated":
        print("[Scenario: Outdated] Pushing newer event first, then older event...")
        sku = PARTNER_SKUS[4]
        wh = WAREHOUSES[0]
        newer_time = now
        older_time = now - timedelta(hours=2)

        # First push newer version
        push_webhook(endpoint, {"items": [generate_mock_event(sku, wh, newer_time, qty_delta=50)]}, api_key=api_key, webhook_secret=secret)
        print("Pushed newer version (physicalQty=150)")

        # Second push older version
        res = push_webhook(endpoint, {"items": [generate_mock_event(sku, wh, older_time, qty_delta=10)]}, api_key=api_key, webhook_secret=secret)
        print(f"Pushed older version: Recorded={res['recorded_changes']}, Outdated={res['ignored_outdated']}")

    elif scenario == "spike":
        import concurrent.futures
        print("[Scenario: Spike Load] Sending 30 concurrent webhook requests...")
        payloads = [
            {"items": [generate_mock_event(random.choice(PARTNER_SKUS), random.choice(WAREHOUSES), now, qty_delta=i)]}
            for i in range(30)
        ]
        start_t = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(push_webhook, endpoint, p, api_key, secret) for p in payloads]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]
        elapsed = time.time() - start_t
        total_rec = sum(r["recorded_changes"] for r in results)
        total_dup = sum(r["ignored_duplicates"] for r in results)
        print(f"Spike completed in {elapsed:.2f}s: Recorded={total_rec}, Duplicates={total_dup}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Emulating Callback Client for CDC Webhook")
    parser.add_argument("--url", default="http://localhost:8000", help="CDMS base URL")
    parser.add_argument(
        "--scenario",
        choices=["normal", "duplicate", "outdated", "spike"],
        default="normal",
        help="Test scenario to execute",
    )
    parser.add_argument("--api-key", default=None, help="CDMS API Key")
    parser.add_argument("--secret", default=None, help="CDMS Webhook HMAC Secret")
    args = parser.parse_args()
    run_scenario(args.url, args.scenario, api_key=args.api_key, secret=args.secret)
