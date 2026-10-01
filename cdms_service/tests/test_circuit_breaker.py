"""Unit tests verifying Circuit Breaker resilience and fault tolerance."""

import time
import httpx
import pytest

from cdms_service.app.services.scheduler import CircuitBreaker


def test_circuit_breaker_initial_state():
    """Verify circuit breaker starts in CLOSED state allowing requests."""
    cb = CircuitBreaker(max_failures=3, reset_timeout=2)
    assert cb.state == "CLOSED"
    assert cb.can_request() is True


def test_circuit_breaker_trips_to_open_on_failures():
    """Verify circuit breaker trips to OPEN after threshold failures."""
    cb = CircuitBreaker(max_failures=3, reset_timeout=2)

    # Simulate 2 failures: still closed
    cb.record_failure(httpx.ConnectError("Connection refused"))
    assert cb.state == "CLOSED"
    assert cb.can_request() is True

    cb.record_failure(httpx.TimeoutException("Read timed out"))
    assert cb.state == "CLOSED"
    assert cb.can_request() is True

    # 3rd failure: trips to OPEN
    cb.record_failure(httpx.HTTPStatusError("500 Server Error", request=None, response=None))
    assert cb.state == "OPEN"
    assert cb.can_request() is False


def test_circuit_breaker_recovers_to_half_open_and_closed():
    """Verify circuit breaker transitions to HALF-OPEN after timeout and resets to CLOSED on success."""
    cb = CircuitBreaker(max_failures=2, reset_timeout=1)

    cb.record_failure(Exception("Fail 1"))
    cb.record_failure(Exception("Fail 2"))
    assert cb.state == "OPEN"
    assert cb.can_request() is False

    # Wait for timeout to expire
    time.sleep(1.1)

    # First request after timeout probes service in HALF_OPEN
    assert cb.can_request() is True
    assert cb.state == "HALF_OPEN"

    # Successful response resets to CLOSED
    cb.record_success()
    assert cb.state == "CLOSED"
    assert cb.failure_count == 0
    assert cb.can_request() is True
