"""Data generator and in-memory store for Vietful Mock Service."""

from datetime import datetime, timezone
import random
from typing import Any, Dict, List, Optional
from faker import Faker

fake = Faker(["vi_VN", "en_US"])

WAREHOUSES = ["WH-HN-01", "WH-HCM-01", "WH-DN-01"]
CATEGORIES = [
    {"code": "ELEC", "name": "Thiết bị điện tử"},
    {"code": "FASH", "name": "Thời trang & Phụ kiện"},
    {"code": "HOME", "name": "Đồ gia dụng & Đời sống"},
    {"code": "COSM", "name": "Mỹ phẩm & Chăm sóc sắc đẹp"},
]
UNITS = ["CAI", "HOP", "BO", "CHAI"]
CONDITIONS = ["NEW", "GOOD", "REFURBISHED"]


class VietfulDataStore:
    """Manages in-memory mock products and inventory states."""

    def __init__(self, initial_count: int = 25) -> None:
        self.products: Dict[str, Dict[str, Any]] = {}
        self.inventories: Dict[str, Dict[str, Any]] = {}
        self._seed_initial_data(initial_count)

    def _seed_initial_data(self, count: int) -> None:
        """Seed realistic inventory products using Faker."""
        for i in range(1, count + 1):
            category = random.choice(CATEGORIES)
            sku = f"SKU-{1000 + i}"
            partner_sku = f"PARTNER-{category['code']}-{100 + i}"
            name = f"{fake.word().capitalize()} {category['name']} {fake.color_name()}"
            unit = random.choice(UNITS)

            # Product Master
            self.products[partner_sku] = {
                "productId": i,
                "sku": sku,
                "partnerSKU": partner_sku,
                "productName": name,
                "assetType": "Single",
                "hasSerial": random.choice([True, False]),
                "hasExpiration": random.choice([True, False]),
                "color": fake.color_name(),
                "size": random.choice(["S", "M", "L", "XL", "Standard"]),
                "description": fake.sentence(nb_words=10),
                "isActive": True,
                "units": [unit],
                "categories": [category],
            }

            # Inventory Records (spread across warehouses)
            wh = random.choice(WAREHOUSES)
            key = f"{wh}#{partner_sku}"
            physical = random.randint(20, 500)
            pending_in = random.randint(0, 30)
            pending_out = random.randint(0, 20)
            freeze = random.randint(0, 5)
            avail = max(0, physical - pending_out - freeze)

            self.inventories[key] = {
                "warehouseCode": wh,
                "partnerSKU": partner_sku,
                "sku": sku,
                "unitCode": unit,
                "conditionTypeCode": "NEW",
                "physicalQty": physical,
                "availableQty": avail,
                "pendingInQty": pending_in,
                "pendingOutQty": pending_out,
                "freezeQty": freeze,
                "inTransitQty": random.randint(0, 10),
                "lastUpdatedDate": datetime.now(timezone.utc).isoformat(),
                "categoryCode": category["code"],
                "categoryName": category["name"],
            }

    def list_products(
        self,
        keyword: Optional[str] = None,
        partner_skus: Optional[str] = None,
        skus: Optional[str] = None,
        page_index: int = 0,
        page_size: int = 10,
    ) -> List[Dict[str, Any]]:
        """Filter and paginate products."""
        items = list(self.products.values())
        if keyword:
            kw = keyword.lower()
            items = [
                p for p in items
                if kw in p["productName"].lower() or kw in p["partnerSKU"].lower() or kw in p["sku"].lower()
            ]
        if partner_skus:
            sku_set = {s.strip() for s in partner_skus.split(",") if s.strip()}
            items = [p for p in items if p["partnerSKU"] in sku_set]
        if skus:
            sku_set = {s.strip() for s in skus.split(",") if s.strip()}
            items = [p for p in items if p["sku"] in sku_set]

        start = page_index * page_size
        return items[start : start + page_size]

    def get_product(self, partner_sku: str) -> Optional[Dict[str, Any]]:
        """Get product detail by partnerSKU."""
        return self.products.get(partner_sku)

    def list_inventories(
        self,
        partner_skus: Optional[str] = None,
        skus: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        page_index: int = 0,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """Filter and paginate inventories."""
        items = list(self.inventories.values())
        if partner_skus:
            sku_set = {s.strip() for s in partner_skus.split(",") if s.strip()}
            items = [i for i in items if i["partnerSKU"] in sku_set]
        if skus:
            sku_set = {s.strip() for s in skus.split(",") if s.strip()}
            items = [i for i in items if i["sku"] in sku_set]
        if from_date:
            try:
                from_dt = datetime.fromisoformat(from_date.replace("Z", "+00:00"))
                if from_dt.tzinfo is None:
                    from_dt = from_dt.replace(tzinfo=timezone.utc)
                items = [
                    i for i in items
                    if datetime.fromisoformat(i["lastUpdatedDate"].replace("Z", "+00:00")) >= from_dt
                ]
            except Exception:
                pass

        total = len(items)
        start = page_index * page_size
        paged_items = items[start : start + page_size]

        return {
            "pageIndex": page_index,
            "pageSize": page_size,
            "totalItems": total,
            "items": paged_items,
        }

    def simulate_changes(self, count: int = 3) -> List[Dict[str, Any]]:
        """Simulate real-world inventory changes for testing delta detection."""
        modified: List[Dict[str, Any]] = []
        keys = list(self.inventories.keys())
        if not keys:
            return modified

        selected_keys = random.sample(keys, min(count, len(keys)))
        for key in selected_keys:
            inv = self.inventories[key]
            delta = random.choice([-10, -5, 5, 12, 20])
            inv["physicalQty"] = max(0, inv["physicalQty"] + delta)
            inv["availableQty"] = max(0, inv["physicalQty"] - inv["pendingOutQty"] - inv["freezeQty"])
            inv["lastUpdatedDate"] = datetime.now(timezone.utc).isoformat()
            modified.append(dict(inv))
        return modified


# Global singleton instance
data_store = VietfulDataStore()
