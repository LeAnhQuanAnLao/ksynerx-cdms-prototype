"""Hashing and timestamp parsing utilities for CDC Engine."""

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, Optional

TRACKED_FIELDS = [
    "sku",
    "unit_code",
    "condition_type_code",
    "physical_qty",
    "available_qty",
    "pending_in_qty",
    "pending_out_qty",
    "freeze_qty",
    "in_transit_qty",
    "is_active",
]


def compute_content_hash(data: Dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of normalized business state fields."""
    normalized = {field: data.get(field) for field in TRACKED_FIELDS}
    serialized = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def parse_timestamp(ts_str: Optional[str]) -> datetime:
    """Parse ISO8601 timestamp string safely, defaulting to current UTC time."""
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)
