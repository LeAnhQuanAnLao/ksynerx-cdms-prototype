"""Tests for Soft Delete, Inactive state handling, and Warehouse Reconciliation."""

from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

from cdms_service.app.database import Base, get_db
from cdms_service.app.main import app
from cdms_service.app.models import (
    CurrentInventoryState,
    InventoryChangeEvent,
    InventoryItemPayload,
    ReconcileRequest,
)
from cdms_service.app.services.cdc_engine import CDCEngine


@pytest.fixture
def db_session():
    """Create isolated in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_delete_action_records_delta_delete(db_session):
    """Verify that incoming item with action='DELETE' records change_type='DELETE' and deactivates state."""
    engine = CDCEngine(db_session)
    now = datetime.now(timezone.utc)
    wh = "WH-HN-01"
    sku = "SKU-DEL-001"

    # 1. Insert active item
    item_v1 = InventoryItemPayload(
        warehouseCode=wh,
        partnerSKU=sku,
        physicalQty=100,
        availableQty=95,
        isActive=True,
        lastUpdatedDate=now.isoformat(),
    )
    res_v1 = engine.process_item(item_v1, source="TEST")
    assert res_v1.change_type == "INSERT"
    assert res_v1.version == 1
    db_session.commit()

    # 2. Update with action='DELETE'
    item_v2 = InventoryItemPayload(
        warehouseCode=wh,
        partnerSKU=sku,
        physicalQty=0,
        availableQty=0,
        action="DELETE",
        lastUpdatedDate=now.isoformat(),
    )
    assert item_v2.is_active is False

    res_v2 = engine.process_item(item_v2, source="TEST")
    assert res_v2.status == "RECORDED_CHANGE"
    assert res_v2.change_type == "DELETE"
    assert res_v2.version == 2
    assert res_v2.diff["is_active"]["old"] is True
    assert res_v2.diff["is_active"]["new"] is False
    db_session.commit()

    # Verify state in DB
    state = db_session.query(CurrentInventoryState).filter_by(warehouse_code=wh, partner_sku=sku).first()
    assert state.is_active is False
    assert state.version == 2


def test_inactive_flag_records_delta_delete(db_session):
    """Verify that transitioning isActive from True to False records change_type='DELETE'."""
    engine = CDCEngine(db_session)
    now = datetime.now(timezone.utc)
    wh = "WH-HCM-01"
    sku = "SKU-INACTIVE-001"

    # Insert active
    item_v1 = InventoryItemPayload(
        warehouseCode=wh,
        partnerSKU=sku,
        physicalQty=50,
        isActive=True,
        lastUpdatedDate=now.isoformat(),
    )
    engine.process_batch([item_v1], source="TEST")

    # Update with isActive=False
    item_v2 = InventoryItemPayload(
        warehouseCode=wh,
        partnerSKU=sku,
        physicalQty=50,
        isActive=False,
        lastUpdatedDate=now.isoformat(),
    )
    report = engine.process_batch([item_v2], source="TEST")
    assert report.recorded_changes == 1
    assert report.results[0].change_type == "DELETE"


def test_reconcile_warehouse_detects_missing_and_records_delete(db_session):
    """Verify catalog reconciliation marks removed items as DELETE while keeping active ones."""
    engine = CDCEngine(db_session)
    now = datetime.now(timezone.utc)
    wh = "WH-DN-01"

    # Seed 3 items
    items = [
        InventoryItemPayload(warehouseCode=wh, partnerSKU="SKU-A", physicalQty=10, isActive=True, lastUpdatedDate=now.isoformat()),
        InventoryItemPayload(warehouseCode=wh, partnerSKU="SKU-B", physicalQty=20, isActive=True, lastUpdatedDate=now.isoformat()),
        InventoryItemPayload(warehouseCode=wh, partnerSKU="SKU-C", physicalQty=30, isActive=True, lastUpdatedDate=now.isoformat()),
    ]
    engine.process_batch(items, source="SEED")

    # Reconcile: catalog now only has SKU-A and SKU-C (SKU-B was removed)
    report = engine.reconcile_warehouse(warehouse_code=wh, active_partner_skus=["SKU-A", "SKU-C"])
    assert report.recorded_changes == 1
    assert report.results[0].partner_sku == "SKU-B"
    assert report.results[0].change_type == "DELETE"

    # Verify SKU-B state is now inactive
    state_b = db_session.query(CurrentInventoryState).filter_by(warehouse_code=wh, partner_sku="SKU-B").first()
    assert state_b.is_active is False
    assert state_b.version == 2

    # Second reconcile with same active list should record 0 new changes
    report2 = engine.reconcile_warehouse(warehouse_code=wh, active_partner_skus=["SKU-A", "SKU-C"])
    assert report2.recorded_changes == 0


def test_reconcile_endpoint_api():
    """Verify POST /api/v1/cdc/reconcile HTTP API endpoint."""
    from sqlalchemy.pool import StaticPool
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
    client = TestClient(app)

    # 1. Insert 2 items via webhook
    client.post(
        "/api/v1/cdc/webhook",
        json={
            "source": "RECON_TEST",
            "items": [
                {"warehouseCode": "WH-TEST", "partnerSKU": "SKU-KEEP", "physicalQty": 10},
                {"warehouseCode": "WH-TEST", "partnerSKU": "SKU-DROP", "physicalQty": 20},
            ],
        },
    )

    # 2. Reconcile with only SKU-KEEP
    resp = client.post(
        "/api/v1/cdc/reconcile",
        json={
            "warehouseCode": "WH-TEST",
            "activePartnerSKUs": ["SKU-KEEP"],
            "source": "RECONCILIATION",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["recorded_changes"] == 1
    assert data["results"][0]["partner_sku"] == "SKU-DROP"
    assert data["results"][0]["change_type"] == "DELETE"

    app.dependency_overrides.clear()
