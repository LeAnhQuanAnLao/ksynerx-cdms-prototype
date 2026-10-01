"""Main entrypoint for Change Data Management Service (CDMS)."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from .api.upload_excel import router as upload_excel_router
from .api.webhook import router as webhook_router
from .config import settings
from .database import check_db_health, init_db, wait_for_db
from .services.scheduler import poller_service

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cdms.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup and graceful shutdown life-cycle manager."""
    logger.info("Starting up %s v%s...", settings.SERVICE_NAME, settings.SERVICE_VERSION)

    # 1. Connect to DB and ensure tables exist
    db_ok = wait_for_db(max_retries=10, delay_seconds=2.0)
    if db_ok:
        init_db()
    else:
        logger.error("Database connection could not be established on startup.")

    # 2. Launch background poller task
    poller_task = None
    if settings.SCHEDULER_ENABLED:
        poller_task = asyncio.create_task(poller_service.start())

    yield

    # 3. Graceful shutdown
    logger.info("Initiating graceful shutdown...")
    if poller_service.is_running:
        poller_service.stop()
    if poller_task:
        poller_task.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass
    logger.info("CDMS Service shutdown complete.")


app = FastAPI(
    title=settings.SERVICE_NAME,
    description="Change Data Management Service prototype for storing only changes of inventory data.",
    version=settings.SERVICE_VERSION,
    lifespan=lifespan,
)

# Secure CORS Configuration (restricting origins to configured hosts)
cors_origins = [origin.strip() for origin in settings.CORS_ORIGINS.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins if cors_origins else ["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Mount Routers
app.include_router(webhook_router)
app.include_router(upload_excel_router)


@app.get("/health", tags=["System"])
def health_check() -> Dict[str, str]:
    """Healthcheck endpoint verifying service and database readiness."""
    db_alive = check_db_health()
    if not db_alive:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection is unavailable",
        )
    return {
        "status": "healthy",
        "service": settings.SERVICE_NAME,
        "version": settings.SERVICE_VERSION,
        "database": "connected",
        "scheduler_enabled": str(settings.SCHEDULER_ENABLED),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
