"""Prometheus metrics endpoint."""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.metrics import metrics_collector
from ..services.scheduler import poller_service

router = APIRouter(tags=["Metrics & Monitoring"])


@router.get("/metrics", response_class=Response)
def get_prometheus_metrics(db: Session = Depends(get_db)) -> Response:
    """Expose Prometheus formatted metrics for scrapers and observability dashboards."""
    cb_state = poller_service.circuit_breaker.state if hasattr(poller_service, "circuit_breaker") else "CLOSED"
    metrics_text = metrics_collector.generate_prometheus_text(db=db, circuit_breaker_state=cb_state)
    return Response(content=metrics_text, media_type="text/plain; version=0.0.4; charset=utf-8")
