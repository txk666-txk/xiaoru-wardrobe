"""Local sandbox catalogs, optionally backed by attributed licensed photos."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent.catalog_images import has_catalog_image
from .base import ProductProvider


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
IMAGE_BY_CATEGORY = {
    "服饰": "/static/images/demo-garment.svg",
    "箱包": "/static/images/demo-bag.svg",
    "包袋": "/static/images/demo-bag.svg",
}


class DemoCatalogProvider(ProductProvider):
    """Loads sandbox items and configured photos without network access."""

    def __init__(self, products_path: Path | str | None = None) -> None:
        self.products_path = Path(products_path or DATA_DIR / "products.json")
        payload = json.loads(self.products_path.read_text(encoding="utf-8"))
        raw_products = payload.get("products")
        if not isinstance(raw_products, list):
            raise ValueError("演示商品文件缺少 products 数组")
        self._products = [self._normalize(item) for item in raw_products if isinstance(item, dict)]
        self._profile = {
            "store_name": "小如的衣橱体验店",
            "subtitle": "原创演示目录 · 本地交易沙盒",
            "dataset_label": "虚构演示商品（不代表真实在售）",
            "snapshot_at": None,
            "source_url": None,
            "ownership_verified": False,
            "transaction_notice": "下单、物流和售后只在本地沙盒模拟。",
        }
        if isinstance(payload.get("store_profile"), dict):
            self._profile.update(deepcopy(payload["store_profile"]))

    @staticmethod
    def _normalize(product: dict[str, Any]) -> dict[str, Any]:
        category = str(product.get("category") or "其他")
        variants = []
        for sku in product.get("skus") or []:
            if not isinstance(sku, dict):
                continue
            stock = sku.get("stock")
            variants.append({
                "sku_id": str(sku.get("sku_id") or ""),
                "attributes": dict(sku.get("attributes") or {}),
                "price": sku.get("price", product.get("price")),
                "stock_public": int(stock) if stock is not None else None,
                "disabled_public": stock is not None and int(stock) <= 0,
            })
        total_stock = sum(item["stock_public"] or 0 for item in variants) if variants else None
        image_url = str(product.get("image_url") or IMAGE_BY_CATEGORY.get(category, "/static/images/demo-garment.svg"))
        image_location = urlparse(image_url)
        photo_available = has_catalog_image(image_url) or bool(
            image_location.scheme == "https" and image_location.hostname
            and not image_location.path.lower().endswith(".svg")
        )
        return {
            "product_id": str(product.get("product_id") or "").upper(),
            "name": str(product.get("name") or "演示商品"),
            "aliases": list(product.get("aliases") or []),
            "subtitle": product.get("subtitle"),
            "selling_point": None,
            "category": category,
            "price": float(product["price"]) if product.get("price") is not None else None,
            "price_max": None,
            "currency": "CNY",
            "image_url": image_url,
            "source_url": product.get("source_url"),
            "public_url": product.get("source_url"),
            "snapshot_at": None,
            "stock_total_public": total_stock,
            "variants": variants,
            "highlights": list(product.get("highlights") or []),
            "weight_ranges": list(product.get("weight_ranges") or []),
            "size_chart_verified": bool(product.get("weight_ranges")),
            "size_chart": {
                "verified": True,
                "verification_scope": "fictional_demo_size_chart",
                "notice": "虚构尺码范围仅用于交互演示，不适用于真实购买。",
                "weight_ranges": list(product.get("weight_ranges") or []),
            } if product.get("weight_ranges") else None,
            "detail_enriched": False,
            "service_labels_public": [],
            "tryon_supported": product.get("tryon_supported") is True and photo_available,
            "tryon_category": product.get("tryon_category"),
            "tryon_garment_photo_type": product.get("tryon_garment_photo_type") or "model",
            "image_attribution": deepcopy(product.get("image_attribution")),
            "demo_only": True,
        }

    def get_store_profile(self) -> dict[str, Any]:
        result = deepcopy(self._profile)
        result["catalog_items_available"] = len(self._products)
        return result

    def list_categories(self) -> list[dict[str, Any]]:
        counts = Counter(item["category"] for item in self._products)
        return [{"name": name, "count": count} for name, count in sorted(counts.items())]

    def search_products(
        self,
        *,
        query: str = "",
        category: str | None = None,
        page: int = 1,
        page_size: int = 12,
    ) -> dict[str, Any]:
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), 50)
        query_tokens = [token.casefold() for token in str(query or "").split() if token]
        matches = []
        for product in self._products:
            if category and product["category"] != category:
                continue
            searchable = " ".join([
                product["product_id"], product["name"], product["category"],
                *(product.get("highlights") or []),
                *(product.get("aliases") or []),
            ]).casefold()
            if all(token in searchable for token in query_tokens):
                matches.append(product)
        total = len(matches)
        start = (page - 1) * page_size
        return {
            "items": deepcopy(matches[start:start + page_size]),
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": (total + page_size - 1) // page_size,
            "query": query,
            "category": category,
        }

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        normalized = str(product_id or "").strip().upper()
        for product in self._products:
            if product["product_id"] == normalized:
                return deepcopy(product)
        return None
