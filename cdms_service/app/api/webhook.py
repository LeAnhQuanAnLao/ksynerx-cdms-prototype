"""Webhook API endpoint for real-time CDC callbacks and delta querying."""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import (
    CurrentInventoryState,
    InventoryChangeEvent,
    WebhookPayload,
    ReconcileRequest,
    CDCProcessReport,
)
from ..security import check_rate_limit, verify_api_key, verify_webhook_hmac
from ..services.cdc_engine import CDCEngine

router = APIRouter(prefix="/api/v1/cdc", tags=["CDC Webhook & Change Data"])


@router.post("/webhook", response_model=CDCProcessReport, status_code=status.HTTP_200_OK)
async def receive_cdc_webhook(
    request: Request,
    payload: WebhookPayload,
    db: Session = Depends(get_db),
    authenticated: bool = Depends(verify_api_key),
    _rate_limit: None = Depends(check_rate_limit),
    x_cdms_signature: Optional[str] = Header(None, alias="X-CDMS-Signature"),
    x_cdms_timestamp: Optional[str] = Header(None, alias="X-CDMS-Timestamp"),
) -> CDCProcessReport:
    """Receive real-time CDC event callback from external inventory services/clients."""
    # Verify HMAC signature and timestamp if signature/timestamp provided or strict auth is configured
    if x_cdms_signature or x_cdms_timestamp or settings.REQUIRE_AUTH:
        body = await request.body()
        verify_webhook_hmac(body, x_cdms_signature, timestamp_header=x_cdms_timestamp)

    if not payload.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload items list cannot be empty",
        )

    engine = CDCEngine(db)
    report = engine.process_batch(
        items=payload.items,
        source=payload.source or "WEBHOOK_CDC",
        client_event_id=payload.event_id,
    )
    return report


@router.post("/reconcile", response_model=CDCProcessReport, status_code=status.HTTP_200_OK)
def reconcile_inventory_catalog(
    payload: ReconcileRequest,
    db: Session = Depends(get_db),
    authenticated: bool = Depends(verify_api_key),
    _rate_limit: None = Depends(check_rate_limit),
) -> CDCProcessReport:
    """Reconcile active inventory catalog against current snapshots, recording DELETE for missing SKUs."""
    engine = CDCEngine(db)
    report = engine.reconcile_warehouse(
        warehouse_code=payload.warehouse_code,
        active_partner_skus=payload.active_partner_skus,
        source=payload.source or "RECONCILIATION",
    )
    return report



@router.get("/events", tags=["Change Data Inspection"])
def list_change_events(
    partner_sku: Optional[str] = Query(None, description="Filter by partnerSKU"),
    warehouse_code: Optional[str] = Query(None, description="Filter by warehouseCode"),
    change_type: Optional[str] = Query(None, description="INSERT or UPDATE"),
    source: Optional[str] = Query(None, description="Filter by ingestion source"),
    page: int = Query(0, ge=0),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    authenticated: bool = Depends(verify_api_key),
    _rate_limit: None = Depends(check_rate_limit),
) -> Dict[str, Any]:
    """Retrieve history of captured delta change events (only new changes)."""
    query = db.query(InventoryChangeEvent)
    if partner_sku:
        query = query.filter(InventoryChangeEvent.partner_sku == partner_sku.strip())
    if warehouse_code:
        query = query.filter(InventoryChangeEvent.warehouse_code == warehouse_code.strip())
    if change_type:
        query = query.filter(InventoryChangeEvent.change_type == change_type.strip().upper())
    if source:
        query = query.filter(InventoryChangeEvent.source == source.strip())

    total = query.count()
    events = (
        query.order_by(InventoryChangeEvent.detected_at.desc())
        .offset(page * size)
        .limit(size)
        .all()
    )

    items = []
    for ev in events:
        items.append({
            "id": ev.id,
            "eventId": ev.event_id,
            "warehouseCode": ev.warehouse_code,
            "partnerSKU": ev.partner_sku,
            "sku": ev.sku,
            "changeType": ev.change_type,
            "source": ev.source,
            "sourceTimestamp": ev.source_timestamp.isoformat() if ev.source_timestamp else None,
            "detectedAt": ev.detected_at.isoformat() if ev.detected_at else None,
            "version": ev.version,
            "diff": ev.diff,
            "oldState": ev.old_state,
            "newState": ev.new_state,
        })

    return {
        "page": page,
        "size": size,
        "total": total,
        "items": items,
    }


@router.get("/states", tags=["Change Data Inspection"])
def list_current_states(
    partner_sku: Optional[str] = Query(None),
    warehouse_code: Optional[str] = Query(None),
    page: int = Query(0, ge=0),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    authenticated: bool = Depends(verify_api_key),
    _rate_limit: None = Depends(check_rate_limit),
) -> Dict[str, Any]:
    """Retrieve latest validated inventory state snapshots."""
    query = db.query(CurrentInventoryState)
    if partner_sku:
        query = query.filter(CurrentInventoryState.partner_sku == partner_sku.strip())
    if warehouse_code:
        query = query.filter(CurrentInventoryState.warehouse_code == warehouse_code.strip())

    total = query.count()
    states = (
        query.order_by(CurrentInventoryState.updated_at.desc())
        .offset(page * size)
        .limit(size)
        .all()
    )

    items = []
    for s in states:
        items.append({
            "warehouseCode": s.warehouse_code,
            "partnerSKU": s.partner_sku,
            "sku": s.sku,
            "productName": s.product_name,
            "unitCode": s.unit_code,
            "conditionTypeCode": s.condition_type_code,
            "physicalQty": s.physical_qty,
            "availableQty": s.available_qty,
            "pendingInQty": s.pending_in_qty,
            "pendingOutQty": s.pending_out_qty,
            "freezeQty": s.freeze_qty,
            "inTransitQty": s.in_transit_qty,
            "isActive": s.is_active,
            "version": s.version,
            "contentHash": s.content_hash,
            "lastEventTimestamp": s.last_event_timestamp.isoformat() if s.last_event_timestamp else None,
            "updatedAt": s.updated_at.isoformat() if s.updated_at else None,
        })

    return {
        "page": page,
        "size": size,
        "total": total,
        "items": items,
    }
