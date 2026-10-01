"""Excel parsing service for batch inventory ingestion."""

import io
import logging
from typing import Any, Dict, List, Optional, Union
import pandas as pd

from ..config import settings
from ..models import InventoryItemPayload

logger = logging.getLogger("cdms.excel_parser")

import unicodedata

# Column alias mappings to accommodate various Excel naming conventions
COLUMN_ALIASES: Dict[str, List[str]] = {
    "warehouse_code": ["warehousecode", "warehouse_code", "warehouse", "kho", "makho", "ma_kho"],
    "partner_sku": ["partnersku", "partner_sku", "partner sku", "masanpham", "ma_san_pham", "skudoitac", "sku_doi_tac", "masp", "ma_sp"],
    "sku": ["sku", "barcode", "mavach", "ma_vach"],
    "product_name": ["productname", "product_name", "product name", "tensanpham", "ten_san_pham", "tensp", "ten_sp"],
    "unit_code": ["unitcode", "unit_code", "unit", "donvi", "don_vi", "dvt"],
    "condition_type_code": ["conditiontypecode", "condition_type_code", "condition", "tinhtrang", "tinh_trang"],
    "physical_qty": ["physicalqty", "physical_qty", "slthucte", "sl_thuc_te", "tonthucte", "ton_thuc_te"],
    "available_qty": ["availableqty", "available_qty", "slkhadung", "sl_kha_dung", "tonkhadung", "ton_kha_dung"],
    "pending_in_qty": ["pendinginqty", "pending_in_qty", "slchonhap", "sl_cho_nhap"],
    "pending_out_qty": ["pendingoutqty", "pending_out_qty", "slchoxuat", "sl_cho_xuat"],
    "freeze_qty": ["freezeqty", "freeze_qty", "sldongbang", "sl_dong_bang"],
    "in_transit_qty": ["intransitqty", "in_transit_qty", "sldangchuyen", "sl_dang_chuyen"],
    "last_updated_date": ["lastupdateddate", "last_updated_date", "ngaycapnhat", "ngay_cap_nhat", "timestamp"],
}


def strip_accents(text: str) -> str:
    """Strip Vietnamese and Unicode accents for uniform matching."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join([c for c in nfkd if not unicodedata.combining(c)])


def normalize_column_name(col: str) -> str:
    """Normalize raw header name to standard key."""
    normalized = strip_accents(str(col)).lower()
    clean = "".join(ch for ch in normalized if ch.isalnum() or ch == "_")
    for standard_name, aliases in COLUMN_ALIASES.items():
        if clean in aliases:
            return standard_name
    return clean


def sanitize_formula_injection(val: Any) -> Optional[str]:
    """Sanitize string values against CSV/Excel formula injection (DDE) by escaping formula prefixes."""
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def parse_excel_file(file_content: Union[bytes, io.BytesIO]) -> List[InventoryItemPayload]:
    """Parse Excel spreadsheet (.xlsx or .xls) into InventoryItemPayload objects."""
    if isinstance(file_content, bytes):
        file_io = io.BytesIO(file_content)
    else:
        file_io = file_content

    try:
        df = pd.read_excel(file_io, engine="openpyxl")
    except Exception as exc:
        logger.error("Failed to read Excel file with openpyxl: %s", exc)
        raise ValueError(f"Invalid Excel file format: {exc}") from exc

    if df.empty:
        return []

    if len(df) > settings.MAX_EXCEL_ROWS:
        raise ValueError(
            f"Excel file exceeds maximum allowed row limit of {settings.MAX_EXCEL_ROWS} rows (found {len(df)} rows)."
        )

    # Map headers to standard field names
    header_mapping = {col: normalize_column_name(col) for col in df.columns}
    df = df.rename(columns=header_mapping)

    # Validate required primary keys
    if "warehouse_code" not in df.columns or "partner_sku" not in df.columns:
        raise ValueError(
            "Excel sheet missing required identifier columns: "
            "'warehouseCode' (or Kho) and 'partnerSKU' (or Mã SP)."
        )

    items: List[InventoryItemPayload] = []
    for idx, row in df.iterrows():
        # Skip rows with empty warehouse or partner_sku
        if pd.isna(row.get("warehouse_code")) or pd.isna(row.get("partner_sku")):
            continue

        raw_item: Dict[str, Any] = {
            "warehouseCode": sanitize_formula_injection(str(row["warehouse_code"]).strip()),
            "partnerSKU": sanitize_formula_injection(str(row["partner_sku"]).strip()),
            "sku": sanitize_formula_injection(str(row.get("sku", row["partner_sku"])).strip()),
            "productName": sanitize_formula_injection(str(row["product_name"]).strip()) if not pd.isna(row.get("product_name")) else None,
            "unitCode": sanitize_formula_injection(str(row.get("unit_code", "CAI")).strip()),
            "conditionTypeCode": sanitize_formula_injection(str(row.get("condition_type_code", "NEW")).strip()),
            "physicalQty": int(row.get("physical_qty", 0)) if not pd.isna(row.get("physical_qty")) else 0,
            "availableQty": int(row.get("available_qty", 0)) if not pd.isna(row.get("available_qty")) else 0,
            "pendingInQty": int(row.get("pending_in_qty", 0)) if not pd.isna(row.get("pending_in_qty")) else 0,
            "pendingOutQty": int(row.get("pending_out_qty", 0)) if not pd.isna(row.get("pending_out_qty")) else 0,
            "freezeQty": int(row.get("freeze_qty", 0)) if not pd.isna(row.get("freeze_qty")) else 0,
            "inTransitQty": int(row.get("in_transit_qty", 0)) if not pd.isna(row.get("in_transit_qty")) else 0,
            "isActive": True,
            "lastUpdatedDate": str(row.get("last_updated_date")) if not pd.isna(row.get("last_updated_date")) else None,
        }

        try:
            items.append(InventoryItemPayload(**raw_item))
        except Exception as exc:
            logger.warning("Skipping invalid row %d: %s", idx + 2, exc)

    logger.info("Successfully parsed %d valid inventory items from Excel.", len(items))
    return items
