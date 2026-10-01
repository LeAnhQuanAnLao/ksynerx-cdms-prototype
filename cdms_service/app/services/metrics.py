"""Prometheus metrics collector and formatter for CDMS."""

from collections import defaultdict
import threading
import time
from typing import Dict, Tuple
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import CurrentInventoryState


class MetricsCollector:
    """Thread-safe collector for CDMS runtime metrics."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.start_time = time.time()
        self.event_counters: Dict[Tuple[str, str], int] = defaultdict(int)
        self.outcome_counters: Dict[str, int] = defaultdict(int)

    def record_change(self, change_type: str, source: str) -> None:
        """Record a genuine delta change event."""
        with self.lock:
            self.event_counters[(change_type.upper(), source)] += 1

    def record_outcome(self, status: str) -> None:
        """Record a record processing outcome (RECORDED, DUPLICATE, OUTDATED)."""
        with self.lock:
            self.outcome_counters[status] += 1

    def generate_prometheus_text(self, db: Session, circuit_breaker_state: str = "CLOSED") -> str:
        """Render metrics in standard Prometheus text exposition format."""
        lines = []
        now = time.time()
        uptime = round(now - self.start_time, 2)

        lines.append("# HELP cdms_uptime_seconds Total seconds since CDMS service started")
        lines.append("# TYPE cdms_uptime_seconds counter")
        lines.append(f"cdms_uptime_seconds {uptime}")

        cb_map = {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}
        cb_val = cb_map.get(circuit_breaker_state.upper(), 0)
        lines.append("# HELP cdms_circuit_breaker_state State of Vietful Circuit Breaker (0=CLOSED, 1=HALF_OPEN, 2=OPEN)")
        lines.append("# TYPE cdms_circuit_breaker_state gauge")
        lines.append(f"cdms_circuit_breaker_state {cb_val}")

        try:
            active_count = db.query(func.count(CurrentInventoryState.partner_sku)).filter(
                CurrentInventoryState.is_active == True
            ).scalar() or 0
        except Exception:
            active_count = 0

        lines.append("# HELP cdms_active_inventory_snapshots Current active inventory items in snapshot database")
        lines.append("# TYPE cdms_active_inventory_snapshots gauge")
        lines.append(f"cdms_active_inventory_snapshots {active_count}")

        lines.append("# HELP cdms_change_events_total Total number of recorded change delta events")
        lines.append("# TYPE cdms_change_events_total counter")
        with self.lock:
            if not self.event_counters:
                lines.append('cdms_change_events_total{change_type="INSERT",source="INITIAL"} 0')
            else:
                for (ctype, src), count in sorted(self.event_counters.items()):
                    lines.append(f'cdms_change_events_total{{change_type="{ctype}",source="{src}"}} {count}')

        lines.append("# HELP cdms_processing_outcomes_total Total processed records by status")
        lines.append("# TYPE cdms_processing_outcomes_total counter")
        with self.lock:
            if not self.outcome_counters:
                lines.append('cdms_processing_outcomes_total{status="NONE"} 0')
            else:
                for st, cnt in sorted(self.outcome_counters.items()):
                    lines.append(f'cdms_processing_outcomes_total{{status="{st}"}} {cnt}')

        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Reset counters for testing."""
        with self.lock:
            self.event_counters.clear()
            self.outcome_counters.clear()
            self.start_time = time.time()


metrics_collector = MetricsCollector()
