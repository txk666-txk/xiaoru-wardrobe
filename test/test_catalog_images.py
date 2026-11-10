"""Local imports must use the same bounded photo path as public samples."""
from __future__ import annotations

import base64
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from agent import catalog_images
from agent.providers.public_youzan_snapshot import PublicYouzanSnapshotProvider
from agent.tryon.providers import FashnLocalTryOnProvider, FashnTryOnProvider, resolve_tryon_reference


class CatalogImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.public = self.root / "public"
        self.local = self.root / "local"
        self.public.mkdir()
        self.local.mkdir()
        stream = io.BytesIO()
        Image.new("RGB", (320, 480), "ivory").save(stream, "JPEG")
        self.photo = stream.getvalue()
        (self.public / "top.jpg").write_bytes(self.photo)
        (self.local / "imported.jpg").write_bytes(self.photo)
        self.enterContext(patch.object(catalog_images, "CATALOG_IMAGE_DIR", self.public))
        self.enterContext(patch.object(catalog_images, "LOCAL_CATALOG_IMAGE_DIR", self.local))

    def test_both_registered_roots_work_without_accepting_other_paths(self):
        for prefix, name, directory in (
            (catalog_images.CATALOG_IMAGE_PREFIX, "top.jpg", self.public),
            (catalog_images.LOCAL_CATALOG_IMAGE_PREFIX, "imported.jpg", self.local),
        ):
            with self.subTest(prefix=prefix):
                self.assertEqual(catalog_images.catalog_image_path(prefix + name), (directory / name).resolve())
                self.assertTrue(catalog_images.has_catalog_image(prefix + name))
                for invalid in ("../top.jpg", "%2e%2e/top.jpg", "nested/top.jpg", "top.jpg?x=1", "missing.jpg"):
                    with self.assertRaises(ValueError):
                        catalog_images.catalog_image_path(prefix + invalid)
        for value in ("file:///private.jpg", "C:/private.jpg", "/static/images/uploads/private.jpg",
                      "https://example.test/image.jpg", "/static/images/catalog-local-other/top.jpg"):
            self.assertIsNone(catalog_images.catalog_image_path(value))

    def test_empty_or_oversized_local_photo_is_rejected(self):
        filename = self.local / "invalid.jpg"
        for content in (b"", b"x" * 2048):
            filename.write_bytes(content)
            with patch.object(catalog_images, "MAX_CATALOG_IMAGE_BYTES", 1024):
                self.assertFalse(catalog_images.has_catalog_image(catalog_images.LOCAL_CATALOG_IMAGE_PREFIX + filename.name))

    def test_imported_product_retains_reference_category_and_photo_type(self):
        raw = {
            "product_id": "LOCAL-1", "name": "修身针织上衣", "category": "卫衣/针织",
            "image_url": catalog_images.LOCAL_CATALOG_IMAGE_PREFIX + "imported.jpg",
            "tryon_image_url": catalog_images.CATALOG_IMAGE_PREFIX + "top.jpg",
            "tryon_category": "tops", "tryon_garment_photo_type": "flat-lay", "color_label": "米色",
            "data_notice": "本机参考图，交易为沙盒演示", "source_type": "local_reference_demo",
        }
        normalized = PublicYouzanSnapshotProvider._normalize_product(raw, {})
        self.assertTrue(normalized["tryon_supported"])
        self.assertEqual(normalized["data_notice"], raw["data_notice"])
        reference = resolve_tryon_reference(normalized)
        self.assertEqual(reference, {"image_url": raw["tryon_image_url"], "color_label": "米色",
                                     "category": "tops", "photo_type": "flat-lay"})
        self.assertFalse(PublicYouzanSnapshotProvider._normalize_product(
            dict(raw, image_url="/static/images/catalog-local/../private.jpg"), {}
        )["tryon_supported"])

    def test_local_and_cloud_providers_send_local_bytes_with_correct_reference(self):
        person = self.root / "person.jpg"
        person.write_bytes(self.photo)
        encoded = base64.b64encode(self.photo).decode("ascii")

        class Http:
            def post(self, _url, **kwargs):
                self.body = kwargs["json"]
                return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "image_base64": encoded, "id": "test"})

            def get(self, _url, **_kwargs):
                return SimpleNamespace(status_code=200, json=lambda: {"status": "completed", "output": [encoded]})

        garment = {"product_id": "LOCAL-1", "image_url": catalog_images.LOCAL_CATALOG_IMAGE_PREFIX + "imported.jpg",
                   "tryon_image_url": catalog_images.CATALOG_IMAGE_PREFIX + "top.jpg",
                   "tryon_category": "bottoms", "tryon_garment_photo_type": "model"}
        for factory in (lambda http: FashnLocalTryOnProvider(http=http),
                        lambda http: FashnTryOnProvider("test-only", http=http, poll_interval=0)):
            http = Http()
            factory(http).generate(person_image=person, garment=garment, beautify="natural", output_path=self.root / "result.jpg")
            body = http.body.get("inputs", http.body)
            if "inputs" not in http.body:
                self.assertEqual(body["category"], "bottoms")
                self.assertEqual(body["garment_photo_type"], "model")
            self.assertEqual(base64.b64decode(body["garment_image"].partition(",")[2]), self.photo)
            self.assertNotIn("garment_image_url", body)


if __name__ == "__main__":
    unittest.main()
