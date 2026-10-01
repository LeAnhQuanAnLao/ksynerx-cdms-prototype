"""Scheduler service for periodic polling from Vietful with Circuit Breaker and Incremental CDC."""

import asyncio
from datetime import datetime, timezone
import logging
import time
from typing import Any, Dict, List, Optional
import httpx

from ..config import settings
from ..database import SessionLocal
from ..models import InventoryItemPayload
from .cdc_engine import CDCEngine

logger = logging.getLogger("cdms.scheduler")


class CircuitBreaker:
    """Protects CDMS from cascading failures when Vietful is down."""

    def __init__(self, max_failures: int = 3, reset_timeout: int = 20) -> None:
        self.max_failures = max_failures
        self.reset_timeout = reset_timeout
        self.failure_count = 0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self.last_failure_time: float = 0.0

    def can_request(self) -> bool:
        """Evaluate if an external request is permitted."""
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if time.time() - self.last_failure_time >= self.reset_timeout:
                self.state = "HALF_OPEN"
                logger.info("CircuitBreaker transitioned to HALF_OPEN (probing Vietful).")
                return True
            return False
        # HALF_OPEN: allow probe
        return True

    def record_success(self) -> None:
        """Reset circuit breaker counters upon successful API call."""
        if self.state != "CLOSED":
            logger.info("CircuitBreaker recovered: state transitioned to CLOSED.")
        self.failure_count = 0
        self.state = "CLOSED"

    def record_failure(self, error: Exception) -> None:
        """Record failure and trip circuit breaker if threshold is exceeded."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        logger.warning(
            "Vietful call failed (count %d/%d): %s",
            self.failure_count,
            self.max_failures,
            error,
        )
        if self.failure_count >= self.max_failures:
            self.state = "OPEN"
            logger.error("CircuitBreaker tripped to OPEN! Pausing Vietful queries for %ds.", self.reset_timeout)


class InventoryPoller:
    """Manages scheduled polling cycles and hands data over to CDC Engine."""

    def __init__(self) -> None:
        self.circuit_breaker = CircuitBreaker(
            max_failures=settings.CIRCUIT_BREAKER_MAX_FAILURES,
            reset_timeout=settings.CIRCUIT_BREAKER_RESET_TIMEOUT,
        )
        self.is_running = False
        self.last_sync_timestamp: Optional[datetime] = None

    async def fetch_vietful_inventories(
        self,
        page_index: int = 0,
        page_size: int = 50,
        from_date: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch single page of inventory list from Vietful mock service with circuit breaker."""
        if not self.circuit_breaker.can_request():
            logger.warning("Circuit breaker OPEN. Skipping polling cycle to avoid request flooding.")
            return None

        url = f"{settings.VIETFUL_API_BASE_URL.rstrip('/')}/api/v1/Products/inventories"
        params: Dict[str, Any] = {"PageIndex": page_index, "PageSize": page_size}
        if from_date:
            params["FromDate"] = from_date

        try:
            async with httpx.AsyncClient(timeout=settings.VIETFUL_REQUEST_TIMEOUT) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                self.circuit_breaker.record_success()
                return data
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            self.circuit_breaker.record_failure(exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error querying Vietful: %s", exc)
            self.circuit_breaker.record_failure(exc)
            return None

    async def fetch_all_inventories(self, from_date: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """Fetch all pages of inventory items using pagination loop."""
        all_items: List[Dict[str, Any]] = []
        page_index = 0
        page_size = 50

        while True:
            data = await self.fetch_vietful_inventories(
                page_index=page_index,
                page_size=page_size,
                from_date=from_date,
            )
            if data is None:
                # Failure recorded by circuit breaker
                return None if not all_items else all_items

            items = data.get("items", [])
            if not items:
                break

            all_items.extend(items)
            total_items = data.get("totalItems", len(all_items))
            page_index += 1

            # Stop when all items collected or partial page returned
            if len(all_items) >= total_items or len(items) < page_size:
                break

        return all_items

    async def poll_once(self) -> None:
        """Execute a single polling and CDC cycle with incremental watermark."""
        logger.debug("Executing scheduled inventory polling cycle...")
        sync_start_time = datetime.now(timezone.utc)
        from_date_str = self.last_sync_timestamp.isoformat() if self.last_sync_timestamp else None

        raw_items = await self.fetch_all_inventories(from_date=from_date_str)
        if raw_items is None or len(raw_items) == 0:
            # Update watermark if fetch was successful but no items changed
            if raw_items is not None:
                self.last_sync_timestamp = sync_start_time
            return

        # Map raw dictionaries to validated DTOs
        payload_items = []
        for item in raw_items:
            try:
                payload_items.append(InventoryItemPayload(**item))
            except Exception as e:
                logger.warning("Failed to validate item from Vietful: %s", e)

        if not payload_items:
            self.last_sync_timestamp = sync_start_time
            return

        # Process with CDC Engine in a dedicated DB session
        db = SessionLocal()
        try:
            engine = CDCEngine(db)
            report = engine.process_batch(payload_items, source="SCHEDULER_POLL")
            if report.recorded_changes > 0:
                logger.info(
                    "Scheduler poll recorded %d new changes (duplicates: %d, outdated: %d)",
                    report.recorded_changes,
                    report.ignored_duplicates,
                    report.ignored_outdated,
                )
            # Advance watermark upon successful processing
            self.last_sync_timestamp = sync_start_time
        except Exception as exc:
            logger.error("Error during CDC batch ingestion in poller: %s", exc)
        finally:
            db.close()

    async def start(self) -> None:
        """Background loop executing scheduled polling."""
        if not settings.SCHEDULER_ENABLED:
            logger.info("Scheduler polling is disabled in configuration.")
            return

        self.is_running = True
        logger.info(
            "Scheduler poller started (Interval: %ds, Target: %s)",
            settings.SCHEDULER_INTERVAL_SECONDS,
            settings.VIETFUL_API_BASE_URL,
        )

        while self.is_running:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Unexpected error in poller loop: %s", exc)

            await asyncio.sleep(settings.SCHEDULER_INTERVAL_SECONDS)

    def stop(self) -> None:
        """Stop background poller."""
        self.is_running = False
        logger.info("Scheduler poller stopped.")


poller_service = InventoryPoller()
