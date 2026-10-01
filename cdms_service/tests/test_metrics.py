"""Tests for Prometheus metrics collection and /metrics HTTP endpoint."""

from starlette.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cdms_service.app.database import Base, get_db
from cdms_service.app.main import app
from cdms_service.app.services.metrics import metrics_collector


def test_prometheus_metrics_endpoint():
    """Verify GET /metrics returns standard Prometheus text format with vital signals."""
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

    # 1. Reset collector
    metrics_collector.reset()

    # 2. Ingest some events via webhook to populate metrics
    resp_wh = client.post(
        "/api/v1/cdc/webhook",
        json={
            "source": "METRICS_SOURCE",
            "items": [
                {"warehouseCode": "WH-HN-01", "partnerSKU": "SKU-METRIC-01", "physicalQty": 100},
                {"warehouseCode": "WH-HN-01", "partnerSKU": "SKU-METRIC-02", "physicalQty": 200},
            ],
        },
    )
    assert resp_wh.status_code == 200

    # 3. Call GET /metrics
    resp_metrics = client.get("/metrics")
    assert resp_metrics.status_code == 200
    assert "text/plain" in resp_metrics.headers["content-type"]

    body = resp_metrics.text
    # Verify core metric lines
    assert "cdms_uptime_seconds" in body
    assert "cdms_circuit_breaker_state" in body
    assert "cdms_active_inventory_snapshots" in body
    assert "cdms_change_events_total" in body
    assert 'change_type="INSERT"' in body
    assert "cdms_processing_outcomes_total" in body
    assert 'status="RECORDED_CHANGE"' in body

    app.dependency_overrides.clear()
