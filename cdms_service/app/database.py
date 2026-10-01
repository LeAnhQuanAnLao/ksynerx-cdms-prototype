"""Database engine, session management, and connectivity handlers."""

import logging
import time
from typing import Generator
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.exc import OperationalError

from .config import settings

logger = logging.getLogger("cdms.database")

# Handle SQLite vs PostgreSQL engine parameters
connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args=connect_args,
        echo=settings.DEBUG,
    )
else:
    engine = create_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        pool_size=15,
        max_overflow=25,
        pool_timeout=30,
        echo=settings.DEBUG,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for yielding database session with auto-rollback on error."""
    db: Session = SessionLocal()
    try:
        yield db
    except Exception as exc:
        db.rollback()
        logger.error("Transaction rolled back due to error: %s", exc)
        raise
    finally:
        db.close()


def check_db_health() -> bool:
    """Check database liveness."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("Database healthcheck failed: %s", exc)
        return False


def wait_for_db(max_retries: int = 10, delay_seconds: float = 2.0) -> bool:
    """Retry connecting to DB on startup to handle container initialization delays."""
    for attempt in range(1, max_retries + 1):
        if check_db_health():
            logger.info("Successfully connected to database.")
            return True
        logger.warning(
            "Waiting for database (attempt %d/%d)... Retrying in %.1fs",
            attempt,
            max_retries,
            delay_seconds,
        )
        time.sleep(delay_seconds)
    logger.error("Could not establish database connection after %d attempts.", max_retries)
    return False


def init_db() -> None:
    """Initialize database tables schema."""
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables initialized successfully.")
