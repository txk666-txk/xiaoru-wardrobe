"""商品数据 Provider。默认开放许可服装演示；本地目录可显式接入。"""

import os
from pathlib import Path

from .base import ProductProvider
from .demo_catalog import DemoCatalogProvider
from .public_youzan_snapshot import PublicYouzanSnapshotProvider

PUBLIC_FASHION_CATALOG = Path(__file__).resolve().parents[1] / "data" / "products_public_fashion.json"

__all__ = ["ProductProvider", "DemoCatalogProvider", "PublicYouzanSnapshotProvider"]


def create_default_product_provider() -> ProductProvider:
    """Prefer an explicitly supplied local snapshot; otherwise use samples."""
    catalog_dir = os.environ.get("FAY_PRODUCT_CATALOG_DIR", "").strip()
    if catalog_dir:
        # An explicitly selected directory must be complete; do not silently
        # substitute samples when an operator supplied the wrong path.
        root = Path(catalog_dir).expanduser()
        return PublicYouzanSnapshotProvider(
            products_path=root / "products_youzan_public.json",
            profile_path=root / "store_profile.json",
        )
    try:
        return PublicYouzanSnapshotProvider()
    except FileNotFoundError:
        return DemoCatalogProvider(PUBLIC_FASHION_CATALOG)


__all__.append("create_default_product_provider")
