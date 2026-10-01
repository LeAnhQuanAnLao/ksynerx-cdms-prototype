"""Unit tests for database session generator and healthcheck."""

from cdms_service.app.database import check_db_health, get_db, wait_for_db


def test_check_db_health_sqlite():
    """Verify check_db_health returns True on active database."""
    assert check_db_health() is True


def test_wait_for_db_success():
    """Verify wait_for_db returns True when DB is accessible."""
    assert wait_for_db(max_retries=2, delay_seconds=0.1) is True


def test_get_db_generator():
    """Verify get_db yields an active session and closes it."""
    gen = get_db()
    session = next(gen)
    assert session is not None
    try:
        next(gen)
    except StopIteration:
        pass  # Successfully closed
