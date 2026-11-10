from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from scripts.import_local_fashion import (
    candidates_from_snapshot, classify, import_catalog, neutral_name, normalize_item,
)


class LocalCatalogImportTests(unittest.TestCase):
    def fixture(self, root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        (source / "store_profile.json").write_text(json.dumps({"store_name": "EXAMPLE示例品牌"}), encoding="utf-8")
        products = [
            {"source_product_alias": "one", "name": "EXAMPLE示例品牌男款复古连帽休闲外套", "category": "其他", "price": 99,
             "image_url": "https://example.test/coat.jpg", "source_url": "https://example.test/store/coat",
             "variants": [{"source_sku_id": "s1", "attributes": {"颜色": "黑色", "尺码": "M 110-140斤"}, "stock_public": 3, "price": 99}]},
            {"source_product_alias": "two", "name": "EXAMPLE示例品牌女款宽松直筒裤", "category": "裤装", "price": 79,
             "image_url": "https://example.test/pants.jpg"},
            {"source_product_alias": "hat", "name": "示例冬季针织帽子", "category": "卫衣/针织", "price": 39,
             "image_url": "https://example.test/hat.jpg"},
        ]
        (source / "products_youzan_public.json").write_text(json.dumps({"products": products}), encoding="utf-8")
        return source

    @staticmethod
    def fake_download(candidate, assets, timeout):
        assets.mkdir(parents=True, exist_ok=True)
        filename = candidate["image_url"].rsplit("/", 1)[1]
        Image.new("RGB", (640, 800), "#bbbbbb").save(assets / filename, "JPEG")
        return {"filename": filename, "width": 640, "height": 800, "sha256": "fixture"}

    def test_clothing_classification_does_not_confuse_hoods_with_hats(self):
        self.assertEqual(classify("男款复古连帽夹克外套"), "外套")
        self.assertEqual(classify("女款翻领长袖衬衫上衣"), "衬衫")
        self.assertEqual(classify("秋季针织毛衣长袖上衣"), "卫衣/针织")
        self.assertEqual(classify("宽松牛仔连体裤"), "裙装/吊带")
        for title in ("冷帽针织帽子", "运动裤腰带", "秋冬手套", "羊毛针织围脖", "纯棉保暖长袜", "卡通贴贴T恤配饰小众饰品"):
            self.assertIsNone(classify(title))

    def test_all_mode_keeps_collaboration_clothing_but_removes_supplied_brand_terms(self):
        dataset = {"products": [{"source_product_alias": "test", "name": "EXAMPLE示例品牌【联名系列】男女同款条纹T恤", "price": 79, "image_url": "https://example.test/tee.jpg"}]}
        candidates = candidates_from_snapshot(dataset, redact_terms=["EXAMPLE", "示例品牌"], exclude_terms=[], skip_aliases=set())
        self.assertEqual(len(candidates), 1)
        self.assertIn("男女", candidates[0]["name"])
        self.assertNotIn("EXAMPLE", candidates[0]["name"])
        self.assertNotIn("联名", candidates[0]["name"])

    def test_normalization_preserves_known_sku_and_never_invents_missing_stock(self):
        raw = {"name": "女款条纹宽松衬衫", "price": 79, "image_url": "https://example.test/shirt.jpg"}
        item, mapping = normalize_item({"raw": raw, "alias": "one", "name": raw["name"], "category": "衬衫", "image_url": raw["image_url"]}, {"filename": "one.jpg"}, [])
        self.assertNotIn("variants", item)
        self.assertNotIn("stock_total_public", item)
        self.assertNotIn("source_url", item)
        self.assertEqual(item["tryon_garment_photo_type"], "model")
        self.assertTrue(item["tryon_supported"])
        self.assertEqual(item["selling_point"], "宽松 · 条纹")
        self.assertEqual(mapping["original_image_url"], raw["image_url"])

    def test_import_all_outputs_only_decoded_clothes_and_private_source_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root)
            original = (source / "products_youzan_public.json").read_bytes()
            out, assets, manifest = root / "out", root / "photos", root / "manifest"
            with patch("scripts.import_local_fashion.download_photo", side_effect=self.fake_download):
                result = import_catalog(source_data=source, output_data=out, assets_dir=assets, manifest_dir=manifest,
                                        count=0, redact_terms=[], exclude_terms=[], skip_aliases=set())
            self.assertEqual(result["products"], 2)
            products = json.loads((out / "products_youzan_public.json").read_text(encoding="utf-8"))["products"]
            self.assertEqual(products[0]["variants"][0]["stock_public"], 3)
            self.assertNotIn("variants", products[1])
            public = (out / "products_youzan_public.json").read_text(encoding="utf-8")
            self.assertNotIn("EXAMPLE", public)
            self.assertNotIn("example.test", public)
            self.assertIn("example.test", (manifest / "source_mapping.json").read_text(encoding="utf-8"))
            self.assertEqual(original, (source / "products_youzan_public.json").read_bytes())
            self.assertTrue((manifest / "contactsheet.jpg").is_file())

    def test_failed_downloads_do_not_replace_a_working_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root)
            out = root / "out"
            out.mkdir()
            previous = out / "products_youzan_public.json"
            previous.write_text("previous catalog", encoding="utf-8")
            with patch("scripts.import_local_fashion.download_photo", side_effect=ValueError("offline")):
                with self.assertRaises(RuntimeError):
                    import_catalog(source_data=source, output_data=out, assets_dir=root / "photos", manifest_dir=root / "manifest",
                                   count=0, redact_terms=[], exclude_terms=[], skip_aliases=set())
            self.assertEqual(previous.read_text(encoding="utf-8"), "previous catalog")

    def test_output_cannot_modify_the_source_snapshot_or_its_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.fixture(root)
            for output in (source, source / "nested", root):
                with self.subTest(output=output), self.assertRaises(ValueError):
                    import_catalog(source_data=source, output_data=output, assets_dir=root / "photos", manifest_dir=root / "manifest",
                                   count=0, redact_terms=[], exclude_terms=[], skip_aliases=set())


if __name__ == "__main__":
    unittest.main()
