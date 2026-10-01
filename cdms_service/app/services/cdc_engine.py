"""Core Change Data Capture (CDC) and Exactly-Once deduplication engine."""

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from ..models import (
    CurrentInventoryState,
    InventoryChangeEvent,
    InventoryItemPayload,
    ProcessItemResult,
    CDCProcessReport,
    ProcessedIdempotency,
)
from .hash_utils import TRACKED_FIELDS, compute_content_hash, parse_timestamp

logger = logging.getLogger("cdms.cdc_engine")


class CDCEngine:
    """Processes incoming inventory records with strict Exactly-Once guarantees."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def process_item(
        self,
        item: InventoryItemPayload,
        source: str,
        client_event_id: Optional[str] = None,
    ) -> ProcessItemResult:
        """Process a single item with row-level transaction locking."""
        wh = item.warehouse_code.strip()
        sku_key = item.partner_sku.strip()
        incoming_data = item.model_dump()
        incoming_hash = compute_content_hash(incoming_data)
        event_time = parse_timestamp(item.last_updated_date)

        # 1. Acquire row-lock on current state
        query = self.db.query(CurrentInventoryState).filter(
            CurrentInventoryState.warehouse_code == wh,
            CurrentInventoryState.partner_sku == sku_key,
        )
        # Apply pessimistic row lock if supported (e.g. Postgres)
        bind = self.db.get_bind()
        if bind and bind.dialect.name != "sqlite":
            query = query.with_for_update()

        current_record = query.first()

        # CASE 1: Brand new product / inventory record
        if not current_record:
            return self._handle_insert(wh, sku_key, item, incoming_hash, event_time, source, client_event_id)

        # CASE 2: Stale / Out-of-order event arriving after newer version
        curr_time = current_record.last_event_timestamp
        if curr_time.tzinfo is None:
            curr_time = curr_time.replace(tzinfo=timezone.utc)

        if event_time < curr_time:
            logger.info("Discarded outdated event for %s#%s (incoming: %s < current: %s)", wh, sku_key, event_time, curr_time)
            return ProcessItemResult(
                warehouse_code=wh,
                partner_sku=sku_key,
                status="IGNORED_OUTDATED",
                version=current_record.version,
                message="Incoming data timestamp is older than current snapshot",
            )

        # CASE 3: Duplicate payload with identical state
        if current_record.content_hash == incoming_hash:
            return ProcessItemResult(
                warehouse_code=wh,
                partner_sku=sku_key,
                status="IGNORED_DUPLICATE",
                version=current_record.version,
                message="Payload is identical to current state (no delta)",
            )

        # CASE 4: Valid modification (delta detected)
        return self._handle_update(current_record, wh, sku_key, item, incoming_hash, event_time, source, client_event_id)

    def _handle_insert(
        self,
        wh: str,
        sku_key: str,
        item: InventoryItemPayload,
        content_hash: str,
        event_time: datetime,
        source: str,
        client_event_id: Optional[str],
    ) -> ProcessItemResult:
        """Handle new item insertion and log delta."""
        new_state_snapshot = {field: getattr(item, field) for field in TRACKED_FIELDS}
        diff_payload = {field: {"old": None, "new": getattr(item, field)} for field in TRACKED_FIELDS}

        new_record = CurrentInventoryState(
            warehouse_code=wh,
            partner_sku=sku_key,
            sku=item.sku or sku_key,
            product_name=item.product_name,
            unit_code=item.unit_code,
            condition_type_code=item.condition_type_code,
            physical_qty=item.physical_qty,
            available_qty=item.available_qty,
            pending_in_qty=item.pending_in_qty,
            pending_out_qty=item.pending_out_qty,
            freeze_qty=item.freeze_qty,
            in_transit_qty=item.in_transit_qty,
            is_active=item.is_active,
            content_hash=content_hash,
            version=1,
            last_event_timestamp=event_time,
            updated_at=datetime.now(timezone.utc),
        )
        self.db.add(new_record)

        # Unique event_id per change event even in batch payloads
        if client_event_id:
            eid = f"{client_event_id}#{wh}#{sku_key}#v1"
        else:
            eid = f"{source}#{wh}#{sku_key}#v1#{content_hash[:8]}"

        change_event = InventoryChangeEvent(
            event_id=eid,
            warehouse_code=wh,
            partner_sku=sku_key,
            sku=item.sku or sku_key,
            change_type="INSERT",
            source=source,
            source_timestamp=event_time,
            detected_at=datetime.now(timezone.utc),
            old_state=None,
            new_state=new_state_snapshot,
            diff=diff_payload,
            content_hash=content_hash,
            version=1,
        )
        self.db.add(change_event)

        return ProcessItemResult(
            warehouse_code=wh,
            partner_sku=sku_key,
            status="RECORDED_CHANGE",
            change_type="INSERT",
            version=1,
            diff=diff_payload,
            message="Recorded new product inventory change (INSERT)",
        )

    def _handle_update(
        self,
        current: CurrentInventoryState,
        wh: str,
        sku_key: str,
        item: InventoryItemPayload,
        content_hash: str,
        event_time: datetime,
        source: str,
        client_event_id: Optional[str],
    ) -> ProcessItemResult:
        """Handle genuine delta update and store granular field difference."""
        old_snapshot = {field: getattr(current, field) for field in TRACKED_FIELDS}
        new_snapshot = {field: getattr(item, field) for field in TRACKED_FIELDS}

        field_diff = {}
        for field in TRACKED_FIELDS:
            old_val = old_snapshot.get(field)
            new_val = new_snapshot.get(field)
            if old_val != new_val:
                field_diff[field] = {"old": old_val, "new": new_val}

        new_version = current.version + 1

        # Update current state
        current.sku = item.sku or current.sku
        if item.product_name:
            current.product_name = item.product_name
        current.unit_code = item.unit_code
        current.condition_type_code = item.condition_type_code
        current.physical_qty = item.physical_qty
        current.available_qty = item.available_qty
        current.pending_in_qty = item.pending_in_qty
        current.pending_out_qty = item.pending_out_qty
        current.freeze_qty = item.freeze_qty
        current.in_transit_qty = item.in_transit_qty
        current.is_active = item.is_active
        current.content_hash = content_hash
        current.version = new_version
        current.last_event_timestamp = event_time
        current.updated_at = datetime.now(timezone.utc)

        # Unique event_id per change event even in batch payloads
        if client_event_id:
            eid = f"{client_event_id}#{wh}#{sku_key}#v{new_version}"
        else:
            eid = f"{source}#{wh}#{sku_key}#v{new_version}#{content_hash[:8]}"

        change_event = InventoryChangeEvent(
            event_id=eid,
            warehouse_code=wh,
            partner_sku=sku_key,
            sku=item.sku or sku_key,
            change_type="UPDATE",
            source=source,
            source_timestamp=event_time,
            detected_at=datetime.now(timezone.utc),
            old_state=old_snapshot,
            new_state=new_snapshot,
            diff=field_diff,
            content_hash=content_hash,
            version=new_version,
        )
        self.db.add(change_event)

        return ProcessItemResult(
            warehouse_code=wh,
            partner_sku=sku_key,
            status="RECORDED_CHANGE",
            change_type="UPDATE",
            version=new_version,
            diff=field_diff,
            message="Recorded delta change (UPDATE)",
        )

    def process_batch(
        self,
        items: List[InventoryItemPayload],
        source: str,
        client_event_id: Optional[str] = None,
    ) -> CDCProcessReport:
        """Process a list of inventory items transactionally with batch idempotency."""
        max_attempts = 4
        for attempt in range(max_attempts):
            # 1. Fast-path Batch Idempotency Check (evaluated on each retry attempt)
            if client_event_id:
                existing_idem = (
                    self.db.query(ProcessedIdempotency)
                    .filter(ProcessedIdempotency.idempotency_key == client_event_id)
                    .first()
                )
                if existing_idem:
                    results = [
                        ProcessItemResult(
                            warehouse_code=item.warehouse_code,
                            partner_sku=item.partner_sku,
                            status="IGNORED_DUPLICATE",
                            message=f"Event ID '{client_event_id}' was already processed",
                        )
                        for item in items
                    ]
                    return CDCProcessReport(
                        total_received=len(items),
                        recorded_changes=0,
                        ignored_duplicates=len(items),
                        ignored_outdated=0,
                        results=results,
                    )

            results: List[ProcessItemResult] = []
            recorded = 0
            duplicates = 0
            outdated = 0
            try:
                for item in items:
                    res = self.process_item(item, source=source, client_event_id=client_event_id)
                    results.append(res)
                    if res.status == "RECORDED_CHANGE":
                        recorded += 1
                        self.db.flush()
                    elif res.status == "IGNORED_DUPLICATE":
                        duplicates += 1
                    elif res.status == "IGNORED_OUTDATED":
                        outdated += 1

                if client_event_id:
                    self.db.merge(ProcessedIdempotency(
                        idempotency_key=client_event_id,
                        source=source,
                        status="RECORDED_CHANGE" if recorded > 0 else "IGNORED_DUPLICATE",
                    ))

                self.db.commit()
                break
            except SQLAlchemyError as exc:
                self.db.rollback()
                self.db.expunge_all()
                if attempt == max_attempts - 1:
                    logger.error("Failed to commit batch after %d attempts: %s", max_attempts, exc)
                    raise
                time.sleep(0.03 * (attempt + 1))
                logger.info("Concurrency conflict (attempt %d/%d), retrying...", attempt + 1, max_attempts)
            except Exception as exc:
                self.db.rollback()
                self.db.expunge_all()
                logger.error("Failed to commit batch: %s", exc)
                raise

        return CDCProcessReport(
            total_received=len(items),
            recorded_changes=recorded,
            ignored_duplicates=duplicates,
            ignored_outdated=outdated,
            results=results,
        )
