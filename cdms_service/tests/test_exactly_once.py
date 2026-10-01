"""Unit tests verifying Exactly-Once semantics and delta change detection."""

from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cdms_service.app.database import Base
from cdms_service.app.models import (
    CurrentInventoryState,
    InventoryChangeEvent,
    InventoryItemPayload,
)
from cdms_service.app.services.cdc_engine import CDCEngine


@pytest.fixture
def db_session():
    """Create isolated in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_insert_new_product_records_delta(db_session):
    """Test Case 1: First time a product is seen -> records INSERT delta."""
    engine = CDCEngine(db_session)
    now = datetime.now(timezone.utc)

    item = InventoryItemPayload(
        warehouseCode="WH-HN-01",
        partnerSKU="SKU-TEST-001",
        sku="BARCODE-001",
        productName="Mechanical Keyboard",
        unitCode="CAI",
        conditionTypeCode="NEW",
        physicalQty=100,
        availableQty=90,
        pendingInQty=10,
        pendingOutQty=5,
        freezeQty=0,
        inTransitQty=0,
        isActive=True,
        lastUpdatedDate=now.isoformat(),
    )

    report = engine.process_batch([item], source="TEST_SUITE")

    assert report.total_received == 1
    assert report.recorded_changes == 1
    assert report.ignored_duplicates == 0
    assert report.results[0].status == "RECORDED_CHANGE"
    assert report.results[0].change_type == "INSERT"
    assert report.results[0].version == 1

    # Verify database state
    state = db_session.query(CurrentInventoryState).first()
    assert state is not None
    assert state.partner_sku == "SKU-TEST-001"
    assert state.version == 1

    # Verify delta log
    events = db_session.query(InventoryChangeEvent).all()
    assert len(events) == 1
    assert events[0].change_type == "INSERT"
    assert events[0].version == 1


def test_duplicate_payload_is_ignored(db_session):
    """Test Case 2: Ingesting the identical payload again -> IGNORED_DUPLICATE (No extra delta)."""
    engine = CDCEngine(db_session)
    now = datetime.now(timezone.utc)

    item = InventoryItemPayload(
        warehouseCode="WH-HN-01",
        partnerSKU="SKU-TEST-002",
        sku="BARCODE-002",
        physicalQty=50,
        availableQty=45,
        lastUpdatedDate=now.isoformat(),
    )

    # First ingestion
    engine.process_batch([item], source="TEST_SUITE")
    assert db_session.query(InventoryChangeEvent).count() == 1

    # Duplicate ingestion
    report2 = engine.process_batch([item], source="TEST_SUITE")
    assert report2.total_received == 1
    assert report2.recorded_changes == 0
    assert report2.ignored_duplicates == 1
    assert report2.results[0].status == "IGNORED_DUPLICATE"

    # Exactly-Once: delta count MUST strictly remain 1!
    assert db_session.query(InventoryChangeEvent).count() == 1


def test_modified_product_records_update_diff(db_session):
    """Test Case 3: Legitimate quantity update -> UPDATE delta recorded with field diff."""
    engine = CDCEngine(db_session)
    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(minutes=5)

    item_v1 = InventoryItemPayload(
        warehouseCode="WH-HN-01",
        partnerSKU="SKU-TEST-003",
        physicalQty=100,
        availableQty=100,
        lastUpdatedDate=t1.isoformat(),
    )
    engine.process_batch([item_v1], source="TEST_SUITE")

    # Update availableQty: 100 -> 80
    item_v2 = InventoryItemPayload(
        warehouseCode="WH-HN-01",
        partnerSKU="SKU-TEST-003",
        physicalQty=100,
        availableQty=80,
        lastUpdatedDate=t2.isoformat(),
    )
    report = engine.process_batch([item_v2], source="TEST_SUITE")

    assert report.recorded_changes == 1
    assert report.results[0].status == "RECORDED_CHANGE"
    assert report.results[0].change_type == "UPDATE"
    assert report.results[0].version == 2
    assert report.results[0].diff == {"available_qty": {"old": 100, "new": 80}}

    # Verify latest state snapshot
    state = db_session.query(CurrentInventoryState).filter_by(partner_sku="SKU-TEST-003").first()
    assert state.version == 2
    assert state.available_qty == 80

    # Total events should be 2 (1 INSERT + 1 UPDATE)
    events = db_session.query(InventoryChangeEvent).filter_by(partner_sku="SKU-TEST-003").all()
    assert len(events) == 2


def test_outdated_event_is_rejected(db_session):
    """Test Case 4: Stale event with older timestamp arriving late -> IGNORED_OUTDATED."""
    engine = CDCEngine(db_session)
    now = datetime.now(timezone.utc)
    past = now - timedelta(hours=1)

    # Ingest newer state first
    item_newer = InventoryItemPayload(
        warehouseCode="WH-HN-01",
        partnerSKU="SKU-TEST-004",
        physicalQty=200,
        lastUpdatedDate=now.isoformat(),
    )
    engine.process_batch([item_newer], source="TEST_SUITE")

    # Ingest older state arriving late
    item_older = InventoryItemPayload(
        warehouseCode="WH-HN-01",
        partnerSKU="SKU-TEST-004",
        physicalQty=50,
        lastUpdatedDate=past.isoformat(),
    )
    report = engine.process_batch([item_older], source="TEST_SUITE")

    assert report.recorded_changes == 0
    assert report.ignored_outdated == 1
    assert report.results[0].status == "IGNORED_OUTDATED"

    # Current state remains unchanged
    state = db_session.query(CurrentInventoryState).filter_by(partner_sku="SKU-TEST-004").first()
    assert state.physical_qty == 200
    assert state.version == 1


def test_event_id_idempotency_check(db_session):
    """Test Case 5: Client-provided event_id replayed -> skipped via idempotency table."""
    engine = CDCEngine(db_session)
    now = datetime.now(timezone.utc)

    item = InventoryItemPayload(
        warehouseCode="WH-HCM-01",
        partnerSKU="SKU-TEST-005",
        physicalQty=75,
        lastUpdatedDate=now.isoformat(),
    )

    report1 = engine.process_batch([item], source="WEBHOOK", client_event_id="EVT-UUID-12345")
    assert report1.recorded_changes == 1

    # Replay same event ID
    report2 = engine.process_batch([item], source="WEBHOOK", client_event_id="EVT-UUID-12345")
    assert report2.recorded_changes == 0
    assert report2.ignored_duplicates == 1
    assert "already processed" in report2.results[0].message


def test_batch_multiple_items_with_event_id(db_session):
    """Test Case 6: Batch of multiple items with single client_event_id does not cause unique constraint collision."""
    engine = CDCEngine(db_session)
    now = datetime.now(timezone.utc)

    items = [
        InventoryItemPayload(
            warehouseCode="WH-HN-01",
            partnerSKU="SKU-BATCH-001",
            physicalQty=100,
            lastUpdatedDate=now.isoformat(),
        ),
        InventoryItemPayload(
            warehouseCode="WH-HN-01",
            partnerSKU="SKU-BATCH-002",
            physicalQty=200,
            lastUpdatedDate=now.isoformat(),
        ),
    ]

    report1 = engine.process_batch(items, source="WEBHOOK", client_event_id="BATCH-EVT-999")
    assert report1.total_received == 2
    assert report1.recorded_changes == 2
    assert report1.ignored_duplicates == 0

    # Verify both change events recorded with distinct event IDs
    events = db_session.query(InventoryChangeEvent).filter(
        InventoryChangeEvent.partner_sku.in_(["SKU-BATCH-001", "SKU-BATCH-002"])
    ).all()
    assert len(events) == 2
    event_ids = {e.event_id for e in events}
    assert len(event_ids) == 2  # Guaranteed distinct!

    # Replay batch: fast-path deduplication
    report2 = engine.process_batch(items, source="WEBHOOK", client_event_id="BATCH-EVT-999")
    assert report2.recorded_changes == 0
    assert report2.ignored_duplicates == 2


def test_batch_with_duplicate_skus_in_same_payload(db_session):
    """Test Case 7: Batch containing multiple updates for the SAME SKU processes sequentially without collision."""
    engine = CDCEngine(db_session)
    now = datetime.now(timezone.utc)
    wh = "WH-HN-01"
    sku = "SKU-INTRA-BATCH-001"

    # Batch with 2 updates for same SKU: first creates, second updates
    items = [
        InventoryItemPayload(
            warehouseCode=wh,
            partnerSKU=sku,
            physicalQty=50,
            lastUpdatedDate=now.isoformat(),
        ),
        InventoryItemPayload(
            warehouseCode=wh,
            partnerSKU=sku,
            physicalQty=75,
            lastUpdatedDate=(now + timedelta(minutes=1)).isoformat(),
        ),
    ]

    report = engine.process_batch(items, source="BATCH_TEST")
    assert report.total_received == 2
    assert report.recorded_changes == 2
    assert report.results[0].status == "RECORDED_CHANGE"
    assert report.results[0].change_type == "INSERT"
    assert report.results[0].version == 1
    assert report.results[1].status == "RECORDED_CHANGE"
    assert report.results[1].change_type == "UPDATE"
    assert report.results[1].version == 2
    assert report.results[1].diff == {"physical_qty": {"old": 50, "new": 75}}

    # Verify final state in DB
    state = db_session.query(CurrentInventoryState).filter_by(warehouse_code=wh, partner_sku=sku).first()
    assert state.version == 2
    assert state.physical_qty == 75


