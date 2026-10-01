"""Data models: SQLAlchemy ORM entities and Pydantic DTO contracts."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Index,
)

from .database import Base


# ==============================================================================
# SQLAlchemy ORM Models
# ==============================================================================

class CurrentInventoryState(Base):
    """Latest validated snapshot for each warehouse inventory item."""

    __tablename__ = "current_inventory_states"

    warehouse_code = Column(String(64), primary_key=True)
    partner_sku = Column(String(128), primary_key=True)
    sku = Column(String(128), nullable=False)
    product_name = Column(String(255), nullable=True)
    unit_code = Column(String(32), nullable=False, default="CAI")
    condition_type_code = Column(String(32), nullable=False, default="NEW")
    physical_qty = Column(Integer, nullable=False, default=0)
    available_qty = Column(Integer, nullable=False, default=0)
    pending_in_qty = Column(Integer, nullable=False, default=0)
    pending_out_qty = Column(Integer, nullable=False, default=0)
    freeze_qty = Column(Integer, nullable=False, default=0)
    in_transit_qty = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    content_hash = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    last_event_timestamp = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class InventoryChangeEvent(Base):
    """Change log table: captures strictly genuine deltas (INSERT / UPDATE / DELETE)."""

    __tablename__ = "inventory_change_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(128), unique=True, nullable=False, index=True)
    warehouse_code = Column(String(64), nullable=False, index=True)
    partner_sku = Column(String(128), nullable=False, index=True)
    sku = Column(String(128), nullable=False)
    change_type = Column(String(16), nullable=False)  # INSERT, UPDATE, DELETE
    source = Column(String(32), nullable=False)        # SCHEDULER_POLL, WEBHOOK_CDC, EXCEL_UPLOAD
    source_timestamp = Column(DateTime(timezone=True), nullable=False)
    detected_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    old_state = Column(JSON, nullable=True)
    new_state = Column(JSON, nullable=False)
    diff = Column(JSON, nullable=False)
    content_hash = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False)

    __table_args__ = (
        Index("ix_change_wh_sku_detected", "warehouse_code", "partner_sku", "detected_at"),
    )


class ProcessedIdempotency(Base):
    """Lookup table to register idempotency keys and fast-skip duplicates."""

    __tablename__ = "processed_events_idempotency"

    idempotency_key = Column(String(128), primary_key=True)
    source = Column(String(32), nullable=False)
    received_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    status = Column(String(32), nullable=False)  # RECORDED_CHANGE, IGNORED_DUPLICATE, IGNORED_OUTDATED


# ==============================================================================
# Pydantic Schemas / DTOs
# ==============================================================================

class InventoryItemPayload(BaseModel):
    """Standardized input contract for an inventory item from any source."""

    warehouse_code: str = Field(..., min_length=1, max_length=64, alias="warehouseCode")
    partner_sku: str = Field(..., min_length=1, max_length=128, alias="partnerSKU")
    sku: Optional[str] = Field(None, max_length=128, alias="sku")
    product_name: Optional[str] = Field(None, max_length=255, alias="productName")
    unit_code: str = Field("CAI", max_length=32, alias="unitCode")
    condition_type_code: str = Field("NEW", max_length=32, alias="conditionTypeCode")
    physical_qty: int = Field(0, ge=0, alias="physicalQty")
    available_qty: int = Field(0, ge=0, alias="availableQty")
    pending_in_qty: int = Field(0, ge=0, alias="pendingInQty")
    pending_out_qty: int = Field(0, ge=0, alias="pendingOutQty")
    freeze_qty: int = Field(0, ge=0, alias="freezeQty")
    in_transit_qty: int = Field(0, ge=0, alias="inTransitQty")
    is_active: bool = Field(True, alias="isActive")
    last_updated_date: Optional[str] = Field(None, alias="lastUpdatedDate")

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def populate_defaults(self) -> "InventoryItemPayload":
        if not self.sku:
            self.sku = self.partner_sku
        return self


class WebhookPayload(BaseModel):
    """Webhook payload pushed by EmulatingCallbackClient."""

    event_id: Optional[str] = Field(None, max_length=128, description="Optional client-provided idempotency key")
    source: str = Field("WEBHOOK_CDC", max_length=32, description="Source identifier")
    timestamp: Optional[str] = Field(None, description="Event generation timestamp")
    items: List[InventoryItemPayload] = Field(..., min_length=1, max_length=2000, description="Batch of inventory items")



class ProcessItemResult(BaseModel):
    """Outcome for a single inventory record processing."""

    warehouse_code: str
    partner_sku: str
    status: str  # RECORDED_CHANGE, IGNORED_DUPLICATE, IGNORED_OUTDATED
    change_type: Optional[str] = None
    version: Optional[int] = None
    diff: Optional[Dict[str, Any]] = None
    message: str


class CDCProcessReport(BaseModel):
    """Aggregated processing summary for a batch of inventory updates."""

    total_received: int
    recorded_changes: int
    ignored_duplicates: int
    ignored_outdated: int
    results: List[ProcessItemResult]
