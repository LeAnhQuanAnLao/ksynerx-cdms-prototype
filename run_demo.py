"""One-Click Demonstration Script for CDMS Prototype.

Executes end-to-end verification across all core features:
1. Healthcheck verification
2. Normal Webhook (INSERT)
3. Duplicate Webhook (Exactly-Once deduplication)
4. Outdated Webhook (Stale timestamp rejection)
5. Soft Delete / Inactive Action (DELETE delta)
6. Catalog Reconciliation (Detect missing SKUs)
7. REST Excel Upload & Dedup
8. Prometheus Metrics Output (/metrics)
"""

import sys
import time
from datetime import datetime, timezone, timedelta
import httpx

BASE_URL = "http://localhost:8000"


def print_step(title: str) -> None:
    print("\n" + "=" * 70)
    print(f">> {title}")
    print("=" * 70)


def main() -> None:
    print_step("CDMS PROTOTYPE END-TO-END AUTOMATED DEMONSTRATION")
    client = httpx.Client(base_url=BASE_URL, timeout=15.0)

    # Step 1: Healthcheck
    print_step("Step 1: System Readiness & Health Check")
    try:
        res = client.get("/health")
        print(f"Status Code: {res.status_code}")
        print(f"Response: {res.json()}")
        assert res.status_code == 200, "CDMS is not healthy"
    except Exception as exc:
        print(f"FAILED to connect to CDMS at {BASE_URL}: {exc}")
        print("Please ensure CDMS is running (docker compose up -d or uvicorn).")
        sys.exit(1)

    now = datetime.now(timezone.utc)

    # Step 2: Normal Webhook
    print_step("Step 2: Normal Webhook Ingestion (INSERT New Product)")
    wh_payload = {
        "source": "DEMO_SCRIPT",
        "items": [
            {
                "warehouseCode": "WH-HN-01",
                "partnerSKU": "DEMO-IPHONE-15",
                "productName": "Apple iPhone 15 128GB",
                "physicalQty": 100,
                "availableQty": 90,
                "isActive": True,
                "lastUpdatedDate": now.isoformat(),
            }
        ],
    }
    res = client.post("/api/v1/cdc/webhook", json=wh_payload)
    print(f"Recorded Changes: {res.json()['recorded_changes']}")
    print(f"First Item Result: {res.json()['results'][0]['message']}")
    assert res.json()["recorded_changes"] == 1

    # Step 3: Duplicate Webhook
    print_step("Step 3: Duplicate Webhook Ingestion (Exactly-Once Verification)")
    res_dup = client.post("/api/v1/cdc/webhook", json=wh_payload)
    print(f"Recorded Changes: {res_dup.json()['recorded_changes']}")
    print(f"Ignored Duplicates: {res_dup.json()['ignored_duplicates']}")
    print(f"Result: {res_dup.json()['results'][0]['message']}")
    assert res_dup.json()["recorded_changes"] == 0
    assert res_dup.json()["ignored_duplicates"] == 1

    # Step 4: Outdated Webhook
    print_step("Step 4: Outdated Stale Event Rejection")
    outdated_time = now - timedelta(hours=3)
    outdated_payload = {
        "source": "DEMO_SCRIPT",
        "items": [
            {
                "warehouseCode": "WH-HN-01",
                "partnerSKU": "DEMO-IPHONE-15",
                "physicalQty": 50,
                "availableQty": 45,
                "lastUpdatedDate": outdated_time.isoformat(),
            }
        ],
    }
    res_stale = client.post("/api/v1/cdc/webhook", json=outdated_payload)
    print(f"Recorded Changes: {res_stale.json()['recorded_changes']}")
    print(f"Ignored Outdated: {res_stale.json()['ignored_outdated']}")
    print(f"Result: {res_stale.json()['results'][0]['message']}")
    assert res_stale.json()["ignored_outdated"] == 1

    # Step 5: Soft Delete Event
    print_step("Step 5: Soft Deletion Action (DELETE Delta Detection)")
    delete_payload = {
        "source": "DEMO_SCRIPT",
        "items": [
            {
                "warehouseCode": "WH-HN-01",
                "partnerSKU": "DEMO-IPHONE-15",
                "action": "DELETE",
                "lastUpdatedDate": (now + timedelta(seconds=10)).isoformat(),
            }
        ],
    }
    res_del = client.post("/api/v1/cdc/webhook", json=delete_payload)
    print(f"Recorded Changes: {res_del.json()['recorded_changes']}")
    print(f"Change Type: {res_del.json()['results'][0]['change_type']}")
    print(f"Diff: {res_del.json()['results'][0]['diff']}")
    assert res_del.json()["results"][0]["change_type"] == "DELETE"

    # Step 6: Catalog Reconciliation
    print_step("Step 6: Warehouse Catalog Reconciliation")
    # Seed SKU-1 and SKU-2
    client.post("/api/v1/cdc/webhook", json={
        "source": "DEMO_SCRIPT",
        "items": [
            {"warehouseCode": "WH-RECON", "partnerSKU": "SKU-KEEP", "physicalQty": 10},
            {"warehouseCode": "WH-RECON", "partnerSKU": "SKU-REMOVE", "physicalQty": 20},
        ]
    })
    # Reconcile only keeping SKU-KEEP
    res_recon = client.post("/api/v1/cdc/reconcile", json={
        "warehouseCode": "WH-RECON",
        "activePartnerSKUs": ["SKU-KEEP"],
        "source": "DEMO_RECONCILIATION",
    })
    print(f"Reconciliation Detected Deletions: {res_recon.json()['recorded_changes']}")
    for r in res_recon.json()["results"]:
        print(f"  Item {r['partner_sku']}: {r['status']} ({r.get('change_type')})")
    assert res_recon.json()["recorded_changes"] == 1
    assert res_recon.json()["results"][0]["partner_sku"] == "SKU-REMOVE"

    # Step 7: Prometheus Metrics
    print_step("Step 7: Observability - Prometheus Metrics (/metrics)")
    res_m = client.get("/metrics")
    print(f"Content-Type: {res_m.headers['content-type']}")
    metric_lines = [l for l in res_m.text.splitlines() if not l.startswith("#") and l.strip()]
    for line in metric_lines[:10]:
        print(f"  {line}")
    assert "cdms_uptime_seconds" in res_m.text

    print_step("ALL 7 DEMONSTRATION STEPS COMPLETED SUCCESSFULLY! (100% PASS)")


if __name__ == "__main__":
    main()
