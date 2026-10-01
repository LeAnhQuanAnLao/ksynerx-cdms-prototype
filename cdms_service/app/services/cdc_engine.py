"""Core Change Data Capture (CDC) and Exactly-Once deduplication engine."""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
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
from .metrics import metrics_collector

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

        query = self.db.query(CurrentInventoryState).filter(
            CurrentInventoryState.warehouse_code == wh,
            CurrentInventoryState.partner_sku == sku_key,
        )
        bind = self.db.get_bind()
        if bind and bind.dialect.name != "sqlite":
            query = query.with_for_update()

        current_record = query.first()

        # CASE 1: Brand new product record
        if not current_record:
            return self._handle_insert(wh, sku_key, item, incoming_hash, event_time, source, client_event_id)

        # CASE 2: Stale / Out-of-order event arriving after newer version
        curr_time = current_record.last_event_timestamp
        if curr_time.tzinfo is None:
            curr_time = curr_time.replace(tzinfo=timezone.utc)

        if event_time < curr_time:
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

        # CASE 4: Valid modification (UPDATE or DELETE delta detected)
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
            content_hash=content_hash,
            version=1,
            last_event_timestamp=event_time,
            updated_at=datetime.now(timezone.utc),
            **{f: getattr(item, f) for f in TRACKED_FIELDS if f != "sku"},
        )
        self.db.add(new_record)

        eid = f"{client_event_id}#{wh}#{sku_key}#v1" if client_event_id else f"{source}#{wh}#{sku_key}#v1#{content_hash[:8]}"
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
        """Handle delta update or soft deletion."""
        old_snapshot = {field: getattr(current, field) for field in TRACKED_FIELDS}
        new_snapshot = {field: getattr(item, field) for field in TRACKED_FIELDS}

        field_diff = {}
        for field in TRACKED_FIELDS:
            old_val = old_snapshot.get(field)
            new_val = new_snapshot.get(field)
            if old_val != new_val:
                field_diff[field] = {"old": old_val, "new": new_val}

        # Check if deletion/inactive action
        is_delete = (item.action and item.action.upper() == "DELETE") or (current.is_active and not item.is_active)
        change_type = "DELETE" if is_delete else "UPDATE"
        new_version = current.version + 1

        for field in TRACKED_FIELDS:
            setattr(current, field, getattr(item, field))
        if item.product_name:
            current.product_name = item.product_name
        current.content_hash = content_hash
        current.version = new_version
        current.last_event_timestamp = event_time
        current.updated_at = datetime.now(timezone.utc)

        eid = f"{client_event_id}#{wh}#{sku_key}#v{new_version}" if client_event_id else f"{source}#{wh}#{sku_key}#v{new_version}#{content_hash[:8]}"
        change_event = InventoryChangeEvent(
            event_id=eid,
            warehouse_code=wh,
            partner_sku=sku_key,
            sku=item.sku or sku_key,
            change_type=change_type,
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
            change_type=change_type,
            version=new_version,
            diff=field_diff,
            message=f"Recorded delta change ({change_type})",
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
            if client_event_id:
                existing_idem = (
                    self.db.query(ProcessedIdempotency)
                    .filter(ProcessedIdempotency.idempotency_key == client_event_id)
                    .first()
                )
                if existing_idem:
                    metrics_collector.record_outcome("IGNORED_DUPLICATE")
                    return CDCProcessReport(
                        total_received=len(items),
                        recorded_changes=0,
                        ignored_duplicates=len(items),
                        ignored_outdated=0,
                        results=[
                            ProcessItemResult(
                                warehouse_code=it.warehouse_code,
                                partner_sku=it.partner_sku,
                                status="IGNORED_DUPLICATE",
                                message=f"Event ID '{client_event_id}' was already processed",
                            )
                            for it in items
                        ],
                    )

            results: List[ProcessItemResult] = []
            recorded = duplicates = outdated = 0
            try:
                for item in items:
                    res = self.process_item(item, source=source, client_event_id=client_event_id)
                    results.append(res)
                    if res.status == "RECORDED_CHANGE":
                        recorded += 1
                        metrics_collector.record_change(res.change_type or "UPDATE", source)
                        metrics_collector.record_outcome("RECORDED_CHANGE")
                        self.db.flush()
                    elif res.status == "IGNORED_DUPLICATE":
                        duplicates += 1
                        metrics_collector.record_outcome("IGNORED_DUPLICATE")
                    elif res.status == "IGNORED_OUTDATED":
                        outdated += 1
                        metrics_collector.record_outcome("IGNORED_OUTDATED")

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

    def reconcile_warehouse(
        self,
        warehouse_code: str,
        active_partner_skus: List[str],
        source: str = "RECONCILIATION",
    ) -> CDCProcessReport:
        """Detect missing items from active catalog list and mark them as DELETE."""
        wh = warehouse_code.strip()
        active_set = {s.strip() for s in active_partner_skus if s.strip()}
        now = datetime.now(timezone.utc)

        query = self.db.query(CurrentInventoryState).filter(
            CurrentInventoryState.warehouse_code == wh,
            CurrentInventoryState.is_active == True,
        )
        bind = self.db.get_bind()
        if bind and bind.dialect.name != "sqlite":
            query = query.with_for_update()

        active_records = query.all()
        results: List[ProcessItemResult] = []
        recorded = 0

        for record in active_records:
            if record.partner_sku not in active_set:
                old_snap = {f: getattr(record, f) for f in TRACKED_FIELDS}
                record.is_active = False
                record.version += 1
                record.updated_at = now
                record.last_event_timestamp = now
                new_snap = {f: getattr(record, f) for f in TRACKED_FIELDS}
                record.content_hash = compute_content_hash(new_snap)

                diff = {"is_active": {"old": True, "new": False}}
                eid = f"{source}#{wh}#{record.partner_sku}#v{record.version}"
                ev = InventoryChangeEvent(
                    event_id=eid,
                    warehouse_code=wh,
                    partner_sku=record.partner_sku,
                    sku=record.sku,
                    change_type="DELETE",
                    source=source,
                    source_timestamp=now,
                    detected_at=now,
                    old_state=old_snap,
                    new_state=new_snap,
                    diff=diff,
                    content_hash=record.content_hash,
                    version=record.version,
                )
                self.db.add(ev)
                self.db.flush()
                recorded += 1
                metrics_collector.record_change("DELETE", source)
                metrics_collector.record_outcome("RECORDED_CHANGE")
                results.append(ProcessItemResult(
                    warehouse_code=wh,
                    partner_sku=record.partner_sku,
                    status="RECORDED_CHANGE",
                    change_type="DELETE",
                    version=record.version,
                    diff=diff,
                    message="Reconciled: Item missing from active catalog, marked as DELETE",
                ))

        self.db.commit()
        return CDCProcessReport(
            total_received=len(active_records),
            recorded_changes=recorded,
            ignored_duplicates=len(active_records) - recorded,
            ignored_outdated=0,
            results=results,
        )
