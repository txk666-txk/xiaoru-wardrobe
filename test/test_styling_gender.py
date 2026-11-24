"""Gender corrections become real catalog filters while preserving outfit constraints."""

import json
import tempfile
import unittest
from pathlib import Path

from agent.providers.demo_catalog import DemoCatalogProvider
from agent.styling.engine import StylingEngine, parse_feedback
from agent.tools.common import matches_requested_gender, product_gender, requested_gender
from test.test_styling import Catalog, product


class GenderFeedbackTests(unittest.TestCase):
    def test_self_correction_preserves_existing_outfit_constraints(self):
        previous = {"scene": "通勤", "style": "简约", "budget": 300, "season": "秋季",
                    "required_colors_by_group": {"tops": ["棕"]}}
        options = parse_feedback("我是男的，你这搭配怎么又有男装又有女装", previous)
        self.assertEqual(options["gender"], "男款")
        for field in previous:
            self.assertEqual(options[field], previous[field])
        self.assertNotIn("gender", previous)

    def test_recipient_takes_priority_over_speakers_gender(self):
        for text, expected in (("我是男的，想给女朋友买外套", "女"),
                               ("我是女的，帮我男朋友选一身", "男"),
                               ("替妈妈买件上衣", "女"),
                               ("给我爸买男装", "男")):
            with self.subTest(text=text):
                self.assertEqual(requested_gender(text), expected)
                self.assertEqual(parse_feedback(text)["gender"], expected + "款")

    def test_explicit_product_preference_can_differ_from_speaker(self):
        self.assertEqual(requested_gender("我是男的，但想看女款外套"), "女")
        self.assertEqual(parse_feedback("我本人是女生，想要男款外套")["gender"], "男款")

    def test_explicit_negative_gender_is_not_selected(self):
        self.assertEqual(requested_gender("不要男装，只要女装"), "女")
        self.assertEqual(requested_gender("不要女款，换成男款"), "男")
        self.assertNotIn("gender", parse_feedback("不要男款"))


class CatalogGenderTests(unittest.TestCase):
    def test_structured_product_gender_takes_priority_over_title(self):
        male = {"name": "女友风棕色衬衫", "gender": "male"}
        female = {"name": "男款营销标题外套", "target_gender": "female"}
        self.assertTrue(matches_requested_gender(male, "男"))
        self.assertFalse(matches_requested_gender(male, "女"))
        self.assertTrue(matches_requested_gender(female, "女款"))
        self.assertFalse(matches_requested_gender(female, "男"))

    def test_structured_sku_and_category_gender_are_supported(self):
        sku = {"name": "基础衬衫", "variants": [{"attributes": {"适用性别": "男士"}}]}
        category = {"name": "基础衬衫", "category_properties_public": {"性别": "女性"}}
        self.assertEqual(product_gender(sku), "男")
        self.assertEqual(product_gender(category), "女")

    def test_unknown_and_conflicting_labels_cannot_satisfy_explicit_gender(self):
        for entry in ({"name": "基础上衣"}, {"name": "基础上衣", "gender": "male", "target_gender": "female"}):
            with self.subTest(entry=entry):
                self.assertIsNone(product_gender(entry))
                self.assertFalse(matches_requested_gender(entry, "男"))
                self.assertFalse(matches_requested_gender(entry, "女"))

    def test_unisex_and_existing_title_labels_remain_supported(self):
        for text in ("中性基础上衣", "情侣外套", "男女同款T恤", "男女休闲裤"):
            with self.subTest(text=text):
                self.assertTrue(matches_requested_gender(text, "男"))
                self.assertTrue(matches_requested_gender(text, "女"))
        self.assertFalse(matches_requested_gender("男友风女款外套", "男"))
        self.assertFalse(matches_requested_gender("男款秋季外套女", "女"))

    def test_provider_preserves_explicit_catalog_gender_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.json"
            path.write_text(json.dumps({"products": [{"product_id": "M1", "name": "基础款", "price": 80,
                                                       "gender": "male", "target_gender": "male",
                                                       "attributes": {"性别": "男"}}]}), encoding="utf-8")
            result = DemoCatalogProvider(path).get_product("M1")
        self.assertEqual(result["gender"], "male")
        self.assertEqual(result["target_gender"], "male")
        self.assertEqual(result["attributes"], {"性别": "男"})


class GenderRecommendationTests(unittest.TestCase):
    def test_male_correction_reruns_with_preserved_budget_scene_and_top_color(self):
        male_top = {**product("M-TOP", "衬衫", 90, "棕色基础衬衫"), "gender": "male"}
        male_bottom = {**product("M-PANTS", "裤装", 100, "黑色直筒长裤"), "target_gender": "male"}
        female_top = product("F-TOP", "衬衫", 60, "女款棕色通勤衬衫")
        female_bottom = product("F-PANTS", "裤装", 70, "女款黑色通勤长裤")
        unknown = product("UNKNOWN", "衬衫", 10, "棕色基础衬衫")
        engine = StylingEngine(Catalog([female_top, female_bottom, male_top, male_bottom, unknown]))
        current = {"scene": "通勤", "budget": 300, "style": "简约",
                   "required_colors_by_group": {"tops": ["棕"]}}
        options = parse_feedback("我是男的，你这搭配怎么又有男装又有女装", current)
        plan = engine.plan(options)
        self.assertTrue(plan["outfits"])
        self.assertEqual(plan["options"]["scene"], "通勤")
        self.assertEqual(plan["options"]["budget"], 300)
        self.assertEqual(plan["options"]["required_colors_by_group"], {"tops": ["棕"]})
        for outfit in plan["outfits"]:
            self.assertEqual({item["product_id"] for item in outfit["items"]}, {"M-TOP", "M-PANTS"})
            self.assertLessEqual(outfit["total"], 300)
            self.assertTrue(all(item["gender"] == "男" for item in outfit["items"]))

    def test_female_recipient_selects_female_or_unisex_items(self):
        products = [product("F-TOP", "衬衫", 70, "女款白色衬衫"),
                    product("F-PANTS", "裤装", 90, "女款黑色长裤"),
                    product("M-TOP", "衬衫", 60, "男款白色衬衫"),
                    product("M-PANTS", "裤装", 80, "男款黑色长裤")]
        options = parse_feedback("我是男的，给女朋友买衣服", {"budget": 300, "scene": "日常"})
        plan = StylingEngine(Catalog(products)).plan(options)
        self.assertTrue(plan["outfits"])
        self.assertTrue(all(item["gender"] == "女" for outfit in plan["outfits"] for item in outfit["items"]))

    def test_no_male_candidates_returns_no_match(self):
        products = [product("F-TOP", "衬衫", 70, "女款棕色衬衫"),
                    product("F-PANTS", "裤装", 90, "女款黑色长裤"),
                    product("UNKNOWN", "衬衫", 10, "棕色基础衬衫")]
        plan = StylingEngine(Catalog(products)).plan({"gender": "男款", "budget": 300})
        self.assertEqual(plan["outfits"], [])
        self.assertIn("未找到", plan["summary"])


if __name__ == "__main__":
    unittest.main()
