"""REST client to generate inventory Excel spreadsheets and upload them to CDMS."""

import argparse
from datetime import datetime, timezone
import io
import os
from typing import Any, Dict, List
import httpx
import pandas as pd
from faker import Faker

fake = Faker(["vi_VN", "en_US"])

SAMPLE_ITEMS = [
    {
        "warehouseCode": "WH-HN-01",
        "partnerSKU": "PARTNER-EXCEL-001",
        "sku": "SKU-EXCEL-001",
        "productName": "Laptop Ultrabook Pro",
        "unitCode": "CAI",
        "conditionTypeCode": "NEW",
        "physicalQty": 50,
        "availableQty": 45,
        "pendingInQty": 10,
        "pendingOutQty": 5,
        "freezeQty": 0,
        "inTransitQty": 0,
        "lastUpdatedDate": datetime.now(timezone.utc).isoformat(),
    },
    {
        "warehouseCode": "WH-HCM-01",
        "partnerSKU": "PARTNER-EXCEL-002",
        "sku": "SKU-EXCEL-002",
        "productName": "Wireless Noise Cancelling Headset",
        "unitCode": "CAI",
        "conditionTypeCode": "NEW",
        "physicalQty": 120,
        "availableQty": 110,
        "pendingInQty": 20,
        "pendingOutQty": 10,
        "freezeQty": 0,
        "inTransitQty": 5,
        "lastUpdatedDate": datetime.now(timezone.utc).isoformat(),
    },
]


def generate_excel_bytes(items: List[Dict[str, Any]]) -> bytes:
    """Create in-memory Excel workbook bytes."""
    df = pd.DataFrame(items)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="InventoryData")
    return buf.getvalue()


def upload_excel(
    cdms_url: str,
    file_bytes: bytes,
    filename: str = "inventory_batch.xlsx",
    api_key: str = None,
) -> Dict[str, Any]:
    """POST excel file to CDMS upload endpoint with optional API key."""
    endpoint = f"{cdms_url.rstrip('/')}/api/v1/cdc/upload-excel"
    files = {"file": (filename, file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(endpoint, files=files, headers=headers)
        resp.raise_for_status()
        return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="REST Excel Client for CDMS Ingestion")
    parser.add_argument("--url", default="http://localhost:8000", help="CDMS base URL")
    parser.add_argument("--api-key", default=None, help="CDMS API Key")
    parser.add_argument("--file", help="Path to existing Excel file (optional)")
    parser.add_argument("--save", help="Path to save generated Excel file locally")
    parser.add_argument("--test-dedup", action="store_true", help="Upload the file twice to verify deduplication")
    args = parser.parse_args()

    if args.file and os.path.exists(args.file):
        print(f"Reading existing Excel file: {args.file}")
        with open(args.file, "rb") as f:
            content = f.read()
        filename = os.path.basename(args.file)
    else:
        print("Generating mock inventory Excel spreadsheet with sample items...")
        content = generate_excel_bytes(SAMPLE_ITEMS)
        filename = "mock_inventory_sample.xlsx"
        if args.save:
            with open(args.save, "wb") as f:
                f.write(content)
            print(f"Saved generated Excel file to: {args.save}")

    print(f"Uploading '{filename}' to CDMS ({args.url})...")
    result = upload_excel(args.url, content, filename, api_key=args.api_key)
    print(f"Upload Result 1:")
    print(f"  Total items: {result['total_received']}")
    print(f"  Recorded changes: {result['recorded_changes']}")
    print(f"  Ignored duplicates: {result['ignored_duplicates']}")

    if args.test_dedup:
        print("\nRe-uploading the exact same file to verify Exactly-Once deduplication...")
        result2 = upload_excel(args.url, content, filename, api_key=args.api_key)
        print(f"Upload Result 2 (Duplicate Check):")
        print(f"  Total items: {result2['total_received']}")
        print(f"  Recorded changes: {result2['recorded_changes']}")
        print(f"  Ignored duplicates: {result2['ignored_duplicates']}")
        assert result2["recorded_changes"] == 0, "Deduplication failed: Recorded unexpected changes!"
        print("SUCCESS: Deduplication verified. Zero duplicate records written.")


if __name__ == "__main__":
    main()
