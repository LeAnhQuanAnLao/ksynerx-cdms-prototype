"""REST API endpoint for uploading and parsing Excel inventory files."""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import CDCProcessReport
from ..security import check_rate_limit, verify_api_key
from ..services.cdc_engine import CDCEngine
from ..services.excel_parser import parse_excel_file

logger = logging.getLogger("cdms.upload_excel")

router = APIRouter(prefix="/api/v1/cdc", tags=["Excel Ingestion"])


@router.post("/upload-excel", response_model=CDCProcessReport, status_code=status.HTTP_200_OK)
async def upload_inventory_excel(
    file: UploadFile = File(..., description="Excel spreadsheet containing inventory rows (.xlsx or .xls)"),
    db: Session = Depends(get_db),
    authenticated: bool = Depends(verify_api_key),
    _rate_limit: None = Depends(check_rate_limit),
) -> CDCProcessReport:
    """Ingest inventory products from an uploaded Excel file with hardening checks."""
    filename = file.filename or ""
    if not (filename.endswith(".xlsx") or filename.endswith(".xls")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file format. Please upload an Excel file (.xlsx or .xls).",
        )

    # 1. Enforce file size limit
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"File exceeds maximum allowed size ({settings.MAX_UPLOAD_SIZE_MB}MB).",
        )

    # 2. Enforce magic bytes verification
    # PK\x03\x04 for .xlsx (Zip-based OOXML), \xd0\xcf\x11\xe0 for legacy .xls (OLE CFBF)
    if filename.endswith(".xlsx") and not content.startswith(b"PK\x03\x04"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file signature (magic bytes). Uploaded file is not a valid OpenXML spreadsheet (.xlsx).",
        )
    elif filename.endswith(".xls") and not content.startswith(b"\xd0\xcf\x11\xe0"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file signature (magic bytes). Uploaded file is not a valid Excel binary spreadsheet (.xls).",
        )

    # 3. Parse spreadsheet safely
    try:
        items = parse_excel_file(content)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except Exception as exc:
        logger.error("Error reading uploaded Excel file: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to parse uploaded Excel file.",
        )

    if not items:
        return CDCProcessReport(
            total_received=0,
            recorded_changes=0,
            ignored_duplicates=0,
            ignored_outdated=0,
            results=[],
        )

    # 4. Ingest via CDC Engine
    engine = CDCEngine(db)
    report = engine.process_batch(items=items, source="EXCEL_UPLOAD")
    return report
