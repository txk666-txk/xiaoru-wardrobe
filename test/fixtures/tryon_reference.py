"""Authored, temporary try-on references shared by isolated regression tests."""
from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent.tryon import providers


REFERENCE_PRODUCT_ID = "DEMO-REFERENCE-TOP"
REFERENCE_IMAGE_URL = "https://example.test/authorized-flat-lay.jpg"


@contextmanager
def temporary_reference_catalog(*, configured: bool = True):
    """Use an isolated authored catalogue; never depend on a user's local files."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        if configured:
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "tryon_references.json").write_text(json.dumps({
                "references": [{
                    "product_id": REFERENCE_PRODUCT_ID,
                    "tryon_image_url": REFERENCE_IMAGE_URL,
                    "tryon_category": "tops",
                    "tryon_garment_photo_type": "flat-lay",
                    "color_label": "粉色",
                }],
            }, ensure_ascii=False), encoding="utf-8")
        providers._verified_tryon_references.cache_clear()
        try:
            with patch.object(providers, "__file__", str(root / "tryon" / "providers.py")):
                yield
        finally:
            providers._verified_tryon_references.cache_clear()

