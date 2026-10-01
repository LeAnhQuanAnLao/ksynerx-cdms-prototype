"""Security, authentication, rate limiting, and hardening test suite."""

import hashlib
import hmac
import io
import json
import time
import pytest
from starlette.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from pydantic import ValidationError

from cdms_service.app.config import settings
from cdms_service.app.database import Base, get_db
from cdms_service.app.main import app
from cdms_service.app.models import InventoryItemPayload
from cdms_service.app.security import rate_limiter, SlidingWindowRateLimiter


@pytest.fixture
def client_with_db():
    """Create TestClient with clean DB and clear rate limiter."""
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
    rate_limiter.reset()

    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    rate_limiter.reset()


def test_hmac_signature_verification_success(client_with_db):
    """Verify webhook accepts valid HMAC-SHA256 signature."""
    payload = {
        "source": "HMAC_TEST",
        "items": [
            {
                "warehouseCode": "WH-HN-01",
                "partnerSKU": "SKU-HMAC-01",
                "physicalQty": 100,
            }
        ],
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()

    resp = client_with_db.post(
        "/api/v1/cdc/webhook",
        content=body_bytes,
        headers={"Content-Type": "application/json", "X-CDMS-Signature": signature},
    )
    assert resp.status_code == 200
    assert resp.json()["recorded_changes"] == 1


def test_hmac_signature_verification_tampered_fails(client_with_db):
    """Verify webhook rejects tampered payload or invalid HMAC signature with 401."""
    payload = {
        "source": "HMAC_TEST",
        "items": [
            {
                "warehouseCode": "WH-HN-01",
                "partnerSKU": "SKU-HMAC-02",
                "physicalQty": 100,
            }
        ],
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    invalid_signature = "invalid_tampered_signature_hex_12345"

    resp = client_with_db.post(
        "/api/v1/cdc/webhook",
        content=body_bytes,
        headers={"Content-Type": "application/json", "X-CDMS-Signature": invalid_signature},
    )
    assert resp.status_code == 401
    assert "Invalid X-CDMS-Signature" in resp.json()["detail"]


def test_rate_limiter_blocks_excessive_requests():
    """Verify SlidingWindowRateLimiter blocks requests exceeding threshold."""
    limiter = SlidingWindowRateLimiter(limit_per_minute=5)
    client_ip = "192.168.1.100"

    # 5 allowed
    for _ in range(5):
        assert limiter.is_allowed(client_ip) is True

    # 6th request blocked
    assert limiter.is_allowed(client_ip) is False


def test_pydantic_validation_rejects_negative_quantity():
    """Verify schema rejects negative inventory quantities (ge=0)."""
    with pytest.raises(ValidationError):
        InventoryItemPayload(
            warehouseCode="WH-TEST",
            partnerSKU="SKU-NEG",
            physicalQty=-50,  # Negative value violates ge=0
        )


def test_excel_upload_rejects_invalid_magic_bytes(client_with_db):
    """Verify upload rejects file with .xlsx extension but fake plain text bytes."""
    fake_content = b"This is not a real zip or xlsx file."
    files = {"file": ("test.xlsx", fake_content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}

    resp = client_with_db.post("/api/v1/cdc/upload-excel", files=files)
    assert resp.status_code == 400
    assert "magic bytes" in resp.json()["detail"]


def test_excel_upload_rejects_oversized_file(client_with_db, monkeypatch):
    """Verify upload rejects file exceeding MAX_UPLOAD_SIZE_MB with 413."""
    from cdms_service.app.config import Settings
    # Temporarily set max upload to 1KB for testing
    monkeypatch.setattr(settings, "MAX_UPLOAD_SIZE_MB", 0)  # 0MB forces size limit

    # Create valid zip header but over limit
    oversized = b"PK\x03\x04" + b"X" * 2000
    files = {"file": ("big.xlsx", oversized, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}

    resp = client_with_db.post("/api/v1/cdc/upload-excel", files=files)
    assert resp.status_code == 413
    assert "exceeds maximum allowed size" in resp.json()["detail"]


def test_hmac_replay_attack_rejected_if_expired(client_with_db):
    """Verify webhook rejects expired timestamp exceeding allowable drift window (Replay Attack)."""
    import time
    payload = {
        "source": "REPLAY_TEST",
        "items": [
            {
                "warehouseCode": "WH-HN-01",
                "partnerSKU": "SKU-REPLAY-01",
                "physicalQty": 100,
            }
        ],
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    expired_ts = str(int(time.time()) - 600)  # 10 minutes ago (drift > 300s)
    signature = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()

    resp = client_with_db.post(
        "/api/v1/cdc/webhook",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-CDMS-Signature": signature,
            "X-CDMS-Timestamp": expired_ts,
        },
    )
    assert resp.status_code == 401
    assert "Replay attack rejected" in resp.json()["detail"]


def test_excel_formula_injection_sanitization():
    """Verify formula injection prefixes (=, +, -, @) are escaped with single quote."""
    from cdms_service.app.services.excel_parser import sanitize_formula_injection
    assert sanitize_formula_injection("=cmd|'/C calc'!A0") == "'=cmd|'/C calc'!A0"
    assert sanitize_formula_injection("+SUM(A1:A10)") == "'+SUM(A1:A10)"
    assert sanitize_formula_injection("@external_link") == "'@external_link"
    assert sanitize_formula_injection("Normal SKU 123") == "Normal SKU 123"
    assert sanitize_formula_injection(None) is None


def test_rate_limiter_evicts_stale_clients():
    """Verify rate limiter memory bounded cleanup evicts inactive clients."""
    limiter = SlidingWindowRateLimiter(limit_per_minute=10, max_clients=10)
    # Add client with timestamp 100s in the past
    limiter.requests["stale_client"] = [time.time() - 100.0]
    assert "stale_client" in limiter.requests

    # Trigger cleanup
    limiter._cleanup(time.time() - 60.0)
    assert "stale_client" not in limiter.requests

