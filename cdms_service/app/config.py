"""Configuration settings for CDMS Service."""

import os
from dataclasses import dataclass


@dataclass
class Settings:
    """Application configuration parameters loaded from environment."""

    # Database connection URI
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql://cdms_user:cdms_password@postgres:5432/cdms_db",
    )

    # Vietful Mock Service URL
    VIETFUL_API_BASE_URL: str = os.getenv(
        "VIETFUL_API_BASE_URL",
        "http://mock_vietful:8001",
    )

    # Scheduler Polling Settings
    SCHEDULER_INTERVAL_SECONDS: int = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "15"))
    SCHEDULER_ENABLED: bool = os.getenv("SCHEDULER_ENABLED", "true").lower() in ("true", "1", "yes")

    # Vietful Failure Handling & Circuit Breaker
    CIRCUIT_BREAKER_MAX_FAILURES: int = int(os.getenv("CIRCUIT_BREAKER_MAX_FAILURES", "3"))
    CIRCUIT_BREAKER_RESET_TIMEOUT: int = int(os.getenv("CIRCUIT_BREAKER_RESET_TIMEOUT", "20"))
    VIETFUL_REQUEST_TIMEOUT: float = float(os.getenv("VIETFUL_REQUEST_TIMEOUT", "5.0"))

    # Security & Authentication Settings
    API_KEY: str = os.getenv("API_KEY", "cdms-secret-api-key-2026")
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "cdms-webhook-secret-2026")
    REQUIRE_AUTH: bool = os.getenv("REQUIRE_AUTH", "false").lower() in ("true", "1", "yes")

    # Rate Limiting & Upload Hardening
    RATE_LIMIT_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
    MAX_UPLOAD_SIZE_MB: int = int(os.getenv("MAX_UPLOAD_SIZE_MB", "10"))
    MAX_EXCEL_ROWS: int = int(os.getenv("MAX_EXCEL_ROWS", "10000"))
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:8000,http://127.0.0.1:8000")

    # Service Metadata
    SERVICE_NAME: str = "CDMS Service"
    SERVICE_VERSION: str = "1.1.0"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")


settings = Settings()

