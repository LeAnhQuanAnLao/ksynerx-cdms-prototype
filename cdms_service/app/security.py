"""Security, authentication, rate limiting, and audit logging utilities."""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import hmac
import logging
import threading
import time
from typing import Optional
from fastapi import Header, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from .config import settings

logger = logging.getLogger("cdms.security")

# Headers
api_key_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


class SlidingWindowRateLimiter:
    """Thread-safe in-memory sliding window rate limiter with auto-eviction to prevent memory exhaustion."""

    def __init__(self, limit_per_minute: int = 120, max_clients: int = 5000) -> None:
        self.limit_per_minute = limit_per_minute
        self.max_clients = max_clients
        self.requests = defaultdict(list)
        self.lock = threading.Lock()

    def _cleanup(self, window_start: float) -> None:
        """Evict stale client entries to keep memory bounded."""
        stale_keys = [
            cid for cid, timestamps in self.requests.items()
            if not timestamps or timestamps[-1] <= window_start
        ]
        for cid in stale_keys:
            del self.requests[cid]

    def is_allowed(self, client_id: str) -> bool:
        """Check if request from client_id is within allowed rate."""
        now = time.time()
        window_start = now - 60.0

        with self.lock:
            # Periodic cleanup if tracked client count exceeds safe threshold
            if len(self.requests) > self.max_clients // 2:
                self._cleanup(window_start)

            # Cap max clients if memory pressure persists
            if len(self.requests) >= self.max_clients and client_id not in self.requests:
                oldest_key = next(iter(self.requests))
                del self.requests[oldest_key]

            # Filter timestamps within last 60 seconds
            valid_timestamps = [ts for ts in self.requests[client_id] if ts > window_start]
            if len(valid_timestamps) >= self.limit_per_minute:
                self.requests[client_id] = valid_timestamps
                return False

            valid_timestamps.append(now)
            self.requests[client_id] = valid_timestamps
            return True

    def reset(self) -> None:
        """Clear rate limiter memory (useful for tests)."""
        with self.lock:
            self.requests.clear()


rate_limiter = SlidingWindowRateLimiter(limit_per_minute=settings.RATE_LIMIT_PER_MINUTE)


def check_rate_limit(request: Request) -> None:
    """FastAPI dependency enforcing rate limiting per client IP."""
    client_ip = request.client.host if request.client else "unknown_client"
    if not rate_limiter.is_allowed(client_ip):
        logger.warning("Rate limit exceeded for client: %s", client_ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({settings.RATE_LIMIT_PER_MINUTE} requests/minute). Please slow down.",
        )


def verify_api_key(
    x_api_key: Optional[str] = Security(api_key_header_scheme),
    authorization: Optional[str] = Header(None),
) -> bool:
    """Verify API Key or Bearer Token."""
    token = x_api_key
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()

    is_valid = token == settings.API_KEY

    if settings.REQUIRE_AUTH:
        if not is_valid:
            logger.warning("Unauthorized access attempt. Provided token: %s", "PRESENT" if token else "NONE")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing authentication credentials (X-API-Key or Bearer token required).",
            )
    else:
        # Permissive dev mode: validate if token is present
        if token and not is_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Provided authentication token is invalid.",
            )

    return True


def verify_webhook_hmac(
    body: bytes,
    signature_header: Optional[str],
    timestamp_header: Optional[str] = None,
    max_drift_seconds: int = 300,
) -> bool:
    """Verify HMAC-SHA256 signature with anti-replay timestamp validation."""
    if not signature_header:
        if settings.REQUIRE_AUTH:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing X-CDMS-Signature header for webhook verification.",
            )
        return True

    # 1. Anti-Replay Check if timestamp header is provided or required
    if timestamp_header:
        try:
            req_ts = float(timestamp_header.strip())
            current_time = time.time()
            if abs(current_time - req_ts) > max_drift_seconds:
                logger.warning(
                    "Webhook rejected: Timestamp %s exceeds allowable drift window (%ds)",
                    timestamp_header,
                    max_drift_seconds,
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Webhook request timestamp outside allowable window ({max_drift_seconds}s). Replay attack rejected.",
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid X-CDMS-Timestamp header. Expected numeric UNIX timestamp.",
            )
    elif settings.REQUIRE_AUTH:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-CDMS-Timestamp header required for Replay Attack protection in strict mode.",
        )

    # 2. Check signature: support direct body signature or timestamp-prepended signature
    sig = signature_header.strip()
    expected_direct = hmac.new(
        settings.WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    expected_with_ts = None
    if timestamp_header:
        expected_with_ts = hmac.new(
            settings.WEBHOOK_SECRET.encode("utf-8"),
            f"{timestamp_header.strip()}.".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()

    valid_sig = hmac.compare_digest(expected_direct, sig) or (
        expected_with_ts is not None and hmac.compare_digest(expected_with_ts, sig)
    )

    if not valid_sig:
        logger.warning("Invalid HMAC signature received for webhook.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-CDMS-Signature header.",
        )
    return True
