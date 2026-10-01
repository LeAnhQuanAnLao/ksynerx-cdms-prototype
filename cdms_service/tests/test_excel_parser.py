"""Unit tests for Excel file parsing and ingestion validation."""

import io
import pandas as pd
import pytest

from cdms_service.app.services.excel_parser import parse_excel_file, normalize_column_name


def test_normalize_column_names():
    """Verify flexible header normalization matching Vietnamese & English aliases."""
    assert normalize_column_name("WarehouseCode") == "warehouse_code"
    assert normalize_column_name("Kho") == "warehouse_code"
    assert normalize_column_name("Partner SKU") == "partner_sku"
    assert normalize_column_name("Mã SP") == "partner_sku"
    assert normalize_column_name("PhysicalQty") == "physical_qty"
    assert normalize_column_name("SL Thực Tế") == "physical_qty"


def test_parse_valid_excel_stream():
    """Verify parsing a valid Excel workbook buffer into InventoryItemPayload models."""
    sample_data = [
        {
            "WarehouseCode": "WH-HN-01",
            "PartnerSKU": "SKU-EX-001",
            "SKU": "BARCODE-001",
            "ProductName": "Smart Speaker",
            "PhysicalQty": 100,
            "AvailableQty": 90,
            "PendingInQty": 10,
            "PendingOutQty": 0,
            "FreezeQty": 0,
            "InTransitQty": 0,
        },
        {
            "WarehouseCode": "WH-HCM-01",
            "PartnerSKU": "SKU-EX-002",
            "SKU": "BARCODE-002",
            "ProductName": "Wireless Mouse",
            "PhysicalQty": 200,
            "AvailableQty": 180,
            "PendingInQty": 20,
            "PendingOutQty": 5,
            "FreezeQty": 0,
            "InTransitQty": 0,
        },
    ]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(sample_data).to_excel(writer, index=False)

    items = parse_excel_file(buf.getvalue())
    assert len(items) == 2
    assert items[0].warehouse_code == "WH-HN-01"
    assert items[0].partner_sku == "SKU-EX-001"
    assert items[0].physical_qty == 100
    assert items[1].warehouse_code == "WH-HCM-01"
    assert items[1].partner_sku == "SKU-EX-002"
    assert items[1].available_qty == 180


def test_parse_excel_missing_required_headers_raises():
    """Verify parser raises descriptive ValueError if required PK headers are missing."""
    invalid_data = [{"SomeRandomCol": 123, "AnotherCol": "abc"}]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(invalid_data).to_excel(writer, index=False)

    with pytest.raises(ValueError, match="missing required identifier columns"):
        parse_excel_file(buf.getvalue())


def test_parse_empty_excel_returns_empty_list():
    """Verify parsing an empty sheet returns an empty list without error."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame().to_excel(writer, index=False)

    assert parse_excel_file(buf.getvalue()) == []


def test_parse_excel_skips_invalid_nan_rows():
    """Verify rows with NaN in primary keys are safely skipped."""
    sample_data = [
        {"WarehouseCode": "WH-HN-01", "PartnerSKU": "SKU-VALID", "PhysicalQty": 10},
        {"WarehouseCode": None, "PartnerSKU": "SKU-INVALID-1", "PhysicalQty": 10},
        {"WarehouseCode": "WH-HN-01", "PartnerSKU": None, "PhysicalQty": 10},
    ]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(sample_data).to_excel(writer, index=False)

    items = parse_excel_file(buf.getvalue())
    assert len(items) == 1
    assert items[0].partner_sku == "SKU-VALID"
