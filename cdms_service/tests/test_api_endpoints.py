"""Integration tests for FastAPI REST endpoints."""

import io
from datetime import datetime, timezone
import pandas as pd
import pytest
from starlette.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cdms_service.app.main import app
from cdms_service.app.database import Base, get_db


@pytest.fixture
def client_with_db():
    """Create a TestClient with an isolated SQLite in-memory database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_health_endpoint(client_with_db):
    """Verify /health reports healthy status."""
    resp = client_with_db.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["database"] == "connected"


def test_webhook_and_inspection_flow(client_with_db):
    """Verify full end-to-end webhook ingestion and subsequent state/event queries."""
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "event_id": "WEBHOOK-TEST-001",
        "source": "WEBHOOK_CLIENT",
        "items": [
            {
                "warehouseCode": "WH-DN-01",
                "partnerSKU": "PARTNER-API-001",
                "sku": "SKU-API-001",
                "productName": "Tablet Ultra",
                "physicalQty": 30,
                "availableQty": 25,
                "lastUpdatedDate": now,
            }
        ],
    }

    # 1. Post webhook
    post_resp = client_with_db.post("/api/v1/cdc/webhook", json=payload)
    assert post_resp.status_code == 200
    report = post_resp.json()
    assert report["recorded_changes"] == 1
    assert report["ignored_duplicates"] == 0

    # 2. Query states endpoint
    states_resp = client_with_db.get("/api/v1/cdc/states")
    assert states_resp.status_code == 200
    states_data = states_resp.json()
    assert states_data["total"] == 1
    assert states_data["items"][0]["partnerSKU"] == "PARTNER-API-001"

    # 3. Query events history endpoint
    events_resp = client_with_db.get("/api/v1/cdc/events")
    assert events_resp.status_code == 200
    events_data = events_resp.json()
    assert events_data["total"] == 1
    assert events_data["items"][0]["changeType"] == "INSERT"


def test_excel_upload_endpoint(client_with_db):
    """Verify uploading .xlsx spreadsheet via multipart/form-data."""
    sample_data = [
        {
            "warehouseCode": "WH-HN-01",
            "partnerSKU": "EXCEL-API-001",
            "sku": "SKU-EX-99",
            "physicalQty": 80,
            "availableQty": 70,
            "lastUpdatedDate": datetime.now(timezone.utc).isoformat(),
        }
    ]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(sample_data).to_excel(writer, index=False)
    buf.seek(0)

    files = {"file": ("test_inventory.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    resp = client_with_db.post("/api/v1/cdc/upload-excel", files=files)
    assert resp.status_code == 200
    report = resp.json()
    assert report["total_received"] == 1
    assert report["recorded_changes"] == 1


def test_webhook_validation_empty_items(client_with_db):
    """Verify webhook rejects empty payload items with HTTP 422/400."""
    resp = client_with_db.post("/api/v1/cdc/webhook", json={"items": []})
    assert resp.status_code in (400, 422)


def test_excel_upload_invalid_extension(client_with_db):
    """Verify upload rejects non-Excel file extensions with HTTP 400."""
    files = {"file": ("test.txt", b"plain text", "text/plain")}
    resp = client_with_db.post("/api/v1/cdc/upload-excel", files=files)
    assert resp.status_code == 400
    assert "Unsupported file format" in resp.json()["detail"]


def test_query_filters(client_with_db):
    """Verify filtering events and states by query parameters."""
    now = datetime.now(timezone.utc).isoformat()
    client_with_db.post(
        "/api/v1/cdc/webhook",
        json={
            "source": "WEBHOOK_FILTER",
            "items": [
                {
                    "warehouseCode": "WH-FILTER-01",
                    "partnerSKU": "SKU-FILT-A",
                    "sku": "SKU-FILT-A",
                    "physicalQty": 10,
                    "lastUpdatedDate": now,
                }
            ],
        },
    )

    # Filter events by partner_sku & warehouse_code & source
    ev_resp = client_with_db.get(
        "/api/v1/cdc/events",
        params={
            "partner_sku": "SKU-FILT-A",
            "warehouse_code": "WH-FILTER-01",
            "change_type": "INSERT",
            "source": "WEBHOOK_FILTER",
        },
    )
    assert ev_resp.status_code == 200
    assert ev_resp.json()["total"] == 1

    # Filter states by warehouse_code
    st_resp = client_with_db.get(
        "/api/v1/cdc/states",
        params={"warehouse_code": "WH-FILTER-01", "partner_sku": "SKU-FILT-A"},
    )
    assert st_resp.status_code == 200
    assert st_resp.json()["total"] == 1
