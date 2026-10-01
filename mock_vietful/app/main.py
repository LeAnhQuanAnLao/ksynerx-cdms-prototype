"""Main FastAPI application for Emulating Vietful Inventory Service."""

from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query, status

from .data_generator import data_store

app = FastAPI(
    title="Emulating Vietful Inventory Service",
    description="Mock implementation of Vietful External API for CDMS prototype evaluation",
    version="1.0.0",
)


@app.get("/health", tags=["System"])
def health_check() -> Dict[str, str]:
    """Healthcheck endpoint for Docker container and orchestrator."""
    return {"status": "ok", "service": "mock_vietful"}


@app.get("/api/v1/Products", tags=["Products"])
def get_products(
    keyword: Optional[str] = Query(None, alias="Keyword", description="SKU or product name"),
    partner_skus: Optional[str] = Query(None, alias="PartnerSKUs", description="Comma-separated PartnerSKUs"),
    skus: Optional[str] = Query(None, alias="SKUs", description="Comma-separated SKUs"),
    page_index: int = Query(0, alias="PageIndex", ge=0),
    page_size: int = Query(10, alias="PageSize", ge=1, le=100),
) -> List[Dict[str, Any]]:
    """Retrieve product catalog matching Vietful specification."""
    return data_store.list_products(
        keyword=keyword,
        partner_skus=partner_skus,
        skus=skus,
        page_index=page_index,
        page_size=page_size,
    )


@app.get("/api/v1/Products/inventories", tags=["Products"])
def get_inventories(
    partner_skus: Optional[str] = Query(None, alias="PartnerSKUs"),
    skus: Optional[str] = Query(None, alias="SKUs"),
    from_date: Optional[str] = Query(None, alias="FromDate"),
    to_date: Optional[str] = Query(None, alias="ToDate"),
    page_index: int = Query(0, alias="PageIndex", ge=0),
    page_size: int = Query(50, alias="PageSize", ge=1, le=200),
) -> Dict[str, Any]:
    """Retrieve product inventories matching Vietful ExtPagedList specification."""
    return data_store.list_inventories(
        partner_skus=partner_skus,
        skus=skus,
        from_date=from_date,
        to_date=to_date,
        page_index=page_index,
        page_size=page_size,
    )


@app.get("/api/v1/Products/{partner_sku}", tags=["Products"])
def get_product_detail(partner_sku: str) -> Dict[str, Any]:
    """Get single product detail by partnerSKU."""
    product = data_store.get_product(partner_sku)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with partnerSKU '{partner_sku}' not found",
        )
    return product


@app.post("/api/v1/Products/simulate-change", tags=["Testing"])
def simulate_change(count: int = Query(3, ge=1, le=20)) -> Dict[str, Any]:
    """Simulate inventory changes on random products for CDC testing."""
    modified = data_store.simulate_changes(count=count)
    return {
        "status": "success",
        "modified_count": len(modified),
        "items": modified,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=False)
