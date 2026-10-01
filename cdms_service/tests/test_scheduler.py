"""Unit tests for InventoryPoller and scheduler ingestion."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import httpx
import pytest

from cdms_service.app.services.scheduler import InventoryPoller


@pytest.mark.anyio
async def test_fetch_vietful_inventories_success():
    """Verify poller successfully fetches and paginates items from Vietful."""
    poller = InventoryPoller()
    mock_items = [
        {
            "warehouseCode": "WH-HN-01",
            "partnerSKU": "SKU-POLL-01",
            "sku": "SKU-POLL-01",
            "physicalQty": 100,
            "availableQty": 90,
            "lastUpdatedDate": datetime.now(timezone.utc).isoformat(),
        }
    ]

    mock_resp = httpx.Response(
        status_code=200,
        json={"pageIndex": 0, "pageSize": 50, "totalItems": 1, "items": mock_items},
        request=httpx.Request("GET", "http://mock_vietful:8001/api/v1/Products/inventories"),
    )

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        items = await poller.fetch_all_inventories()
        assert items is not None
        assert len(items) == 1
        assert items[0]["partnerSKU"] == "SKU-POLL-01"
        assert poller.circuit_breaker.state == "CLOSED"


@pytest.mark.anyio
async def test_fetch_vietful_inventories_error_handled_by_circuit_breaker():
    """Verify connection error records failure in circuit breaker without raising exception."""
    poller = InventoryPoller()

    with patch("httpx.AsyncClient.get", side_effect=httpx.ConnectError("Connection refused")):
        items = await poller.fetch_all_inventories()
        assert items is None
        assert poller.circuit_breaker.failure_count == 1


@pytest.mark.anyio
async def test_poll_once_runs_cdc_engine_and_advances_watermark():
    """Verify poll_once passes fetched items into CDC Engine and updates watermark."""
    poller = InventoryPoller()
    mock_items = [
        {
            "warehouseCode": "WH-HCM-01",
            "partnerSKU": "SKU-POLL-02",
            "sku": "SKU-POLL-02",
            "physicalQty": 50,
            "availableQty": 40,
            "lastUpdatedDate": datetime.now(timezone.utc).isoformat(),
        }
    ]

    with patch.object(poller, "fetch_all_inventories", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mock_items
        assert poller.last_sync_timestamp is None
        await poller.poll_once()
        assert mock_fetch.called
        assert poller.last_sync_timestamp is not None
