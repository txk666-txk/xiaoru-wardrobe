"""Shipped clothing assets must remain usable, attributable and try-on ready."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from agent.catalog_images import catalog_image_path
from agent.providers import create_default_product_provider
from agent.store.repository import DemoCommerceRepository
from agent.store.service import CommerceService
from agent.styling.engine import StylingEngine
from agent.tools.product_tool import ProductTool
from agent.tryon.providers import FashnLocalTryOnProvider, TryOnProviderError


PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT / "agent" / "data"


class PublicFashionCatalogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        with patch.dict(os.environ, {"FAY_PRODUCT_CATALOG_DIR": ""}), patch(
            "agent.providers.PublicYouzanSnapshotProvider", side_effect=FileNotFoundError
        ):
            self.provider = create_default_product_provider()
        self.service = CommerceService(
            provider=self.provider,
            repository=DemoCommerceRepository(self.directory / "catalog.db"),
        )
        self.manifest = json.loads((DATA / "public_fashion_sources.json").read_text(encoding="utf-8"))
        self.products = self.provider.search_products(page_size=50)["items"]
        self.garments = [item for item in self.products if item["tryon_supported"]]

    def test_every_shipped_garment_has_matching_license_hash_and_real_photo(self):
        self.assertEqual(len(self.garments), 8)
        sources = {record["product_id"]: record for record in self.manifest["sources"]}
        self.assertEqual(set(sources), {item["product_id"] for item in self.garments})
        for garment in self.garments:
            with self.subTest(product_id=garment["product_id"]):
                source = sources[garment["product_id"]]
                path = catalog_image_path(garment["image_url"])
                self.assertEqual(garment["image_url"], source["image_url"])
                self.assertEqual(path, (PROJECT / source["local_path"]).resolve())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source["sha256"])
                with Image.open(path) as photo:
                    self.assertEqual(photo.format, "JPEG")
                    self.assertEqual(photo.size, (source["width"], source["height"]))
                    self.assertGreaterEqual(min(photo.size), 320)
                for field in ("author", "source", "original_url", "license_url", "processing", "attribution_text"):
                    self.assertTrue(source[field], field)
                self.assertIn(source["license"], {"CC0", "CC BY 2.0", "CC BY 4.0"})
                self.assertTrue(source["source"].startswith("https://commons.wikimedia.org/wiki/File:"))
                self.assertTrue(source["license_url"].startswith("https://creativecommons.org/"))
                # Image credits are never offered as a merchant or purchase URL.
                self.assertIsNone(garment["source_url"])
                self.assertIsNone(garment["public_url"])

    def test_default_catalog_contains_only_real_clothing_without_legacy_placeholders(self):
        self.assertEqual(len(self.products), 8)
        self.assertEqual(self.provider.search_products(page_size=50)["total"], 8)
        self.assertEqual(len(self.garments), len(self.products))
        self.assertEqual({item["product_id"] for item in self.products},
                         {f"EC{value}" for value in range(1005, 1013)})
        for product in self.products:
            self.assertFalse(product["image_url"].endswith(".svg"))
            self.assertTrue(product["image_attribution"])
        for legacy_id in ("EC1001", "EC1002", "EC1004"):
            self.assertIsNone(self.provider.get_product(legacy_id))

    def test_storefront_bootstrap_preserves_image_credits(self):
        products = self.service.storefront()["products"]
        self.assertEqual(len(products), 8)
        for product in products:
            original = self.provider.get_product(product["product_id"])
            self.assertEqual(product["image_attribution"], original["image_attribution"])
            self.assertTrue(product["image_attribution"]["author"])
            self.assertTrue(product["image_attribution"]["license_url"])

    def test_day_to_day_mens_outfit_under_200_has_real_tryon_photos(self):
        plan = StylingEngine(self.service).plan({
            "budget": 200, "gender": "男款", "scene": "日常", "season": "夏季",
        })
        self.assertTrue(plan["outfits"])
        for outfit in plan["outfits"]:
            self.assertLessEqual(outfit["total"], 200)
            self.assertEqual({item["group"] for item in outfit["items"]}, {"tops", "bottoms"})
            self.assertTrue(all(item["tryon_supported"] for item in outfit["items"]))
            self.assertTrue(all(item["source_url"] is None for item in outfit["items"]))

    def test_product_tool_uses_same_names_prices_budget_and_gender_as_store(self):
        with patch("agent.tools.product_tool.create_default_product_provider", return_value=self.provider):
            tool = ProductTool()
        result = tool.execute(query="推荐男装上衣，预算100元以内")
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["items"])
        for item in result["data"]["items"]:
            self.assertLessEqual(item["price"], 100)
            self.assertIn("中性", item["name"])
            self.assertTrue(item["tryon_supported"])
        self.assertIn("沙盒", result["message"])
        specific = tool.execute(query="EC1005有货吗", product_id="EC1005")
        self.assertTrue(specific["ok"])
        self.assertIn(self.provider.get_product("EC1005")["name"], specific["message"])

    def test_all_eight_garments_send_shipped_bytes_and_correct_metadata_to_fashn(self):
        stream = io.BytesIO()
        Image.new("RGB", (320, 480), "white").save(stream, format="JPEG")
        person = self.directory / "person.jpg"
        person.write_bytes(stream.getvalue())
        output_base64 = base64.b64encode(stream.getvalue()).decode("ascii")

        class Http:
            def post(self, _url, **kwargs):
                self.body = kwargs["json"]
                class Response:
                    status_code = 200

                    def json(self):
                        return {"ok": True, "image_base64": output_base64}
                return Response()

        for garment in self.garments:
            with self.subTest(product_id=garment["product_id"]):
                http = Http()
                FashnLocalTryOnProvider(http=http).generate(
                    person_image=person, garment=garment, beautify="natural",
                    output_path=self.directory / "result.jpg",
                )
                self.assertEqual(http.body["category"], garment["tryon_category"])
                self.assertEqual(http.body["garment_photo_type"], "flat-lay")
                encoded = http.body["garment_image"]
                self.assertNotIn("garment_image_url", http.body)
                self.assertTrue(encoded.startswith("data:image/jpeg;base64,"))
                self.assertEqual(base64.b64decode(encoded.partition(",")[2]),
                                 catalog_image_path(garment["image_url"]).read_bytes())

    def test_catalog_paths_reject_traversal_missing_files_and_unlisted_file_schemes(self):
        for name in ("../private.jpg", "%2e%2e/private.jpg", "folder/top.jpg", "missing.jpg", "gray-tee.jpg?x=1"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                catalog_image_path("/static/images/catalog/" + name)
        for path in ("C:/private.jpg", "file:///private.jpg", "/static/private.jpg", "https://example.test/top.jpg"):
            self.assertIsNone(catalog_image_path(path))
        person = self.directory / "person.jpg"
        person.write_bytes(b"not-read-before-reference-validation")
        with self.assertRaises(TryOnProviderError) as caught:
            FashnLocalTryOnProvider().generate(
                person_image=person, garment={"image_url": "/static/images/catalog/../private.jpg"},
                beautify="natural", output_path=self.directory / "result.jpg",
            )
        self.assertEqual(caught.exception.code, "garment_image_unavailable")


if __name__ == "__main__":
    unittest.main()
