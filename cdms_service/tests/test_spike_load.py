"""Spike and concurrency load tests for CDMS Engine under true concurrent execution."""

import concurrent.futures
from datetime import datetime, timezone
import random
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cdms_service.app.database import Base
from cdms_service.app.models import (
    CurrentInventoryState,
    InventoryChangeEvent,
    InventoryItemPayload,
)
from cdms_service.app.services.cdc_engine import CDCEngine


@pytest.fixture
def db_factory(tmp_path):
    """Create a temporary file-based SQLite database with WAL mode for true concurrent multi-thread testing."""
    db_file = tmp_path / "concurrent_spike.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False, "timeout": 30.0},
    )
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def _get_session():
        return TestingSession()

    return _get_session


def test_concurrent_spike_identical_payloads(db_factory):
    """Spike test: 50 concurrent worker threads pushing identical payload simultaneously without locks."""
    sku = "SPIKE-IDENTICAL-001"
    wh = "WH-HN-01"
    now = datetime.now(timezone.utc)

    def worker_push(worker_id: int):
        db = db_factory()
        try:
            engine = CDCEngine(db)
            item = InventoryItemPayload(
                warehouseCode=wh,
                partnerSKU=sku,
                sku=sku,
                physicalQty=100,
                availableQty=95,
                lastUpdatedDate=now.isoformat(),
            )
            client_id = f"BURST-IDEM-{sku}"
            return engine.process_batch([item], source="SPIKE_TEST", client_event_id=client_id)
        finally:
            db.close()

    # Launch 50 workers concurrently across thread pool
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker_push, i) for i in range(50)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    total_recorded = sum(r.recorded_changes for r in results)
    total_duplicates = sum(r.ignored_duplicates for r in results)

    # Exactly-Once: Across all 50 concurrent requests, exactly ONE change must be recorded!
    assert total_recorded == 1
    assert total_duplicates == 49

    # Verify DB state
    db = db_factory()
    state = db.query(CurrentInventoryState).filter_by(partner_sku=sku).first()
    assert state.version == 1
    events = db.query(InventoryChangeEvent).filter_by(partner_sku=sku).all()
    assert len(events) == 1
    db.close()


def test_concurrent_spike_distinct_skus(db_factory):
    """Spike test: 100 concurrent worker threads across 20 distinct products without artificial locks."""
    num_skus = 20
    skus = [f"SPIKE-SKU-{i:03d}" for i in range(num_skus)]
    wh = "WH-HCM-01"
    now = datetime.now(timezone.utc)

    def worker_push(worker_id: int):
        db = db_factory()
        try:
            chosen_sku = random.choice(skus)
            engine = CDCEngine(db)
            item = InventoryItemPayload(
                warehouseCode=wh,
                partnerSKU=chosen_sku,
                sku=chosen_sku,
                physicalQty=150,
                availableQty=140,
                lastUpdatedDate=now.isoformat(),
            )
            return engine.process_batch([item], source="SPIKE_BATCH")
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker_push, i) for i in range(100)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    total_recorded = sum(r.recorded_changes for r in results)
    total_duplicates = sum(r.ignored_duplicates for r in results)

    assert total_recorded + total_duplicates == 100

    # There are at most 20 distinct SKUs, so total recorded changes can never exceed 20!
    assert total_recorded <= num_skus

    db = db_factory()
    assert db.query(CurrentInventoryState).count() == total_recorded
    assert db.query(InventoryChangeEvent).count() == total_recorded
    db.close()
