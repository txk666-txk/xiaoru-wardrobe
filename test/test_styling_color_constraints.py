"""Hard per-category color and explicit budget reset regression coverage."""
import tempfile
import unittest

from agent.styling.engine import StylingEngine, parse_feedback, colors_of
from agent.styling.preferences import clean_preferences, merge_preferences
from agent.styling.service import StylingService
from agent.styling.vision import StylingError


def garment(pid, category, name, price):
    return {"product_id": pid, "category": category, "name": name, "price": price,
            "stock_total_public": 3, "image_url": "/sample.jpg"}


class Catalog:
    def __init__(self, products):
        self.products = products

    def list_products(self, **kwargs):
        return {"items": self.products, "pages": 1}


class StylingColorConstraintTests(unittest.TestCase):
    def setUp(self):
        self.products = [garment("RED-TOP", "T恤/上衣", "红色简约上衣", 240),
                         garment("WHITE-TOP", "衬衫", "白色简约衬衫", 250),
                         garment("BLACK-PANTS", "裤装", "黑色直筒裤子", 200),
                         garment("BLUE-PANTS", "裤装", "蓝色直筒裤子", 260),
                         garment("WHITE-COAT", "外套", "白色简约外套", 210),
                         garment("BLACK-COAT", "外套", "黑色简约外套", 270)]
        self.engine = StylingEngine(Catalog(self.products))

    def test_original_feedback_clears_old_budget_and_keeps_scoped_colors(self):
        options = parse_feedback("给我再挑一套不以黑色为裤子，不以白色为外套，不以红色为内搭的穿搭，价格不限",
                                 {"scene": "通勤", "season": "秋季", "budget": 300})
        self.assertNotIn("budget", options)
        self.assertTrue(options["budget_unlimited"])
        self.assertEqual(options["excluded_colors_by_group"],
                         {"bottoms": ["黑"], "outerwear": ["白"], "tops": ["红"]})
        self.assertNotIn("excluded_colors", options)
        plan = self.engine.plan(options)
        self.assertTrue(plan["outfits"])
        self.assertNotIn("budget", plan["options"])
        self.assertGreater(plan["outfits"][0]["total"], 300)
        for outfit in plan["outfits"]:
            self.assertEqual({item["product_id"] for item in outfit["items"]},
                             {"WHITE-TOP", "BLUE-PANTS", "BLACK-COAT"})

    def test_alternative_word_orders_do_not_turn_scoped_exclusions_global(self):
        for question in ("裤子不要黑色，外套别选白色，不要红色内搭，预算不限",
                         "不要黑色裤子，白色外套不用，内搭不选红色，价格不限"):
            with self.subTest(question=question):
                options = parse_feedback(question)
                self.assertEqual(options["excluded_colors_by_group"]["bottoms"], ["黑"])
                self.assertEqual(options["excluded_colors_by_group"]["outerwear"], ["白"])
                self.assertEqual(options["excluded_colors_by_group"]["tops"], ["红"])
                self.assertNotIn("excluded_colors", options)

    def test_global_and_scoped_colors_can_be_combined(self):
        options = parse_feedback("不要绿色，裤子不要黑色，外套别选白色")
        self.assertEqual(options["excluded_colors"], ["绿"])
        self.assertEqual(options["excluded_colors_by_group"], {"bottoms": ["黑"], "outerwear": ["白"]})

    def test_explicit_number_replaces_unlimited_and_unlimited_replaces_number(self):
        options = parse_feedback("预算500", {"budget_unlimited": True})
        self.assertEqual(options["budget"], 500)
        self.assertNotIn("budget_unlimited", options)
        self.assertNotIn("budget", parse_feedback("价格不限", options))
        self.assertEqual(merge_preferences({"budget_unlimited": True}, {"budget": 500}), {"budget": 500})
        self.assertEqual(merge_preferences({"budget": 500}, {"budget_unlimited": True}), {"budget_unlimited": True})

    def test_scoped_colors_persist_and_can_be_cleared_for_one_group(self):
        first = parse_feedback("裤子不要黑色，外套不要白色")
        next_turn = parse_feedback("预算500", first)
        self.assertEqual(next_turn["excluded_colors_by_group"], first["excluded_colors_by_group"])
        cleared = parse_feedback("裤子颜色不限", next_turn)
        self.assertEqual(cleared["excluded_colors_by_group"], {"outerwear": ["白"]})
        self.assertEqual(first["excluded_colors_by_group"], {"bottoms": ["黑"], "outerwear": ["白"]})
        self.assertEqual(merge_preferences(first, {"excluded_colors_by_group": {"tops": ["红"]}})
                         ["excluded_colors_by_group"], {"bottoms": ["黑"], "outerwear": ["白"], "tops": ["红"]})

    def test_named_outerwear_is_required_even_without_cold_temperature(self):
        options = parse_feedback("外套不要白色，价格不限")
        plan = self.engine.plan(options)
        self.assertTrue(plan["outfits"])
        for outfit in plan["outfits"]:
            coats = [item for item in outfit["items"] if item["group"] == "outerwear"]
            self.assertEqual(len(coats), 1)
            self.assertEqual(coats[0]["product_id"], "BLACK-COAT")

    def test_jacket_and_inner_layer_colors_follow_their_own_categories(self):
        engine = StylingEngine(Catalog(self.products + [garment("BROWN-COAT", "外套", "棕色夹克", 220)]))
        options = parse_feedback("帮我推荐一套衣服，上衣要棕色的外套，内搭要白色，裤子颜色随意，预算800",
                                 {"required_colors_by_group": {"bottoms": ["红"]}})
        self.assertEqual(options["required_colors_by_group"], {"outerwear": ["棕"], "tops": ["白"]})
        outfits = engine.plan(options)["outfits"]
        self.assertTrue(outfits)
        for outfit in outfits:
            items = {item["group"]: item["product_id"] for item in outfit["items"]}
            self.assertEqual(items["outerwear"], "BROWN-COAT")
            self.assertEqual(items["tops"], "WHITE-TOP")
            self.assertIn(items["bottoms"], {"BLACK-PANTS", "BLUE-PANTS"})

    def test_dress_cannot_bypass_explicit_top_and_pants_request(self):
        engine = StylingEngine(Catalog([
            garment("DRESS", "裙装/吊带", "女款白色简约连衣裙", 20),
            garment("TOP", "衬衫", "女款白色简约衬衫", 90),
            garment("PANTS", "裤装", "女款蓝色直筒裤子", 90)]))
        options = parse_feedback("内搭不要红色，裤子不要黑色，价格不限", {"gender": "女款"})
        plan = engine.plan(options)
        self.assertTrue(plan["outfits"])
        for outfit in plan["outfits"]:
            self.assertEqual({item["group"] for item in outfit["items"]}, {"tops", "bottoms"})

    def test_unknown_color_is_not_claimed_to_meet_an_exclusion(self):
        products = [garment("TOP", "衬衫", "简约衬衫", 40),
                    garment("PANTS", "裤装", "直筒裤子", 50)]
        plan = StylingEngine(Catalog(products)).plan({"excluded_colors_by_group": {"bottoms": ["黑"]}})
        self.assertFalse(plan["outfits"])

    def test_owned_excluded_bottoms_are_replaced_without_replacing_other_owned_groups(self):
        observation = {"mode": "garment", "garments": [
            {"category": "tops", "color": "白"}, {"category": "bottoms", "color": "黑"}]}
        plan = self.engine.plan({"budget_unlimited": True,
                                 "excluded_colors_by_group": {"bottoms": ["黑"]}}, observation)
        self.assertTrue(plan["outfits"])
        self.assertEqual(plan["replaced_owned_groups"], ["bottoms"])
        self.assertIn("黑色裤装与本次颜色要求冲突", plan["summary"])
        for outfit in plan["outfits"]:
            self.assertEqual(outfit["owned_items"], [{"category": "tops", "color": "白"}])
            self.assertEqual([item["product_id"] for item in outfit["items"]], ["BLUE-PANTS"])
            self.assertIn("重新筛选裤装", outfit["reason"])
        # Keep the original visual evidence intact for a subsequent refinement.
        self.assertEqual(observation["garments"][1]["color"], "黑")

    def test_owned_unknown_color_cannot_satisfy_a_constrained_group(self):
        observation = {"mode": "garment", "garments": [
            {"category": "tops", "color": "白"}, {"category": "bottoms", "color": "未知"}]}
        plan = self.engine.plan({"budget_unlimited": True,
                                 "excluded_colors_by_group": {"bottoms": ["黑"]}}, observation)
        self.assertTrue(plan["outfits"])
        self.assertIn("颜色无法核实", plan["summary"])
        self.assertEqual(plan["replaced_owned_groups"], ["bottoms"])
        self.assertTrue(all(item["category"] != "bottoms" for item in plan["outfits"][0]["owned_items"]))
        self.assertEqual(plan["outfits"][0]["items"][0]["product_id"], "BLUE-PANTS")

    def test_unreplaceable_owned_exclusion_is_not_reported_as_complete(self):
        engine = StylingEngine(Catalog([self.products[0], self.products[2]]))
        observation = {"mode": "garment", "garments": [
            {"category": "tops", "color": "白"}, {"category": "bottoms", "color": "黑"}]}
        plan = engine.plan({"budget_unlimited": True,
                            "excluded_colors_by_group": {"bottoms": ["黑"]}}, observation)
        self.assertFalse(plan["outfits"])
        self.assertIn("未找到", plan["summary"])
        self.assertNotIn("已覆盖", plan["summary"])

    def test_material_names_do_not_invent_a_color_and_sku_attributes_take_priority(self):
        self.assertEqual(colors_of({"name": "90白鸭绒工装羽绒裤"}), [])
        self.assertEqual(colors_of({"name": "黑科技红外保暖外套"}), [])
        self.assertEqual(colors_of({"name": "黑色白鸭绒羽绒裤"}), ["黑"])
        self.assertEqual(colors_of({"name": "白鸭绒红外保暖外套", "variants": [
            {"attributes": {"颜色": "藏青蓝", "尺码": "M"}}]}), ["蓝"])

    def test_no_compatible_color_returns_no_outfit_without_relaxing_request(self):
        engine = StylingEngine(Catalog([self.products[0], self.products[2]]))
        plan = engine.plan({"budget_unlimited": True, "excluded_colors_by_group": {"bottoms": ["黑"]}})
        self.assertFalse(plan["outfits"])
        self.assertIn("未找到", plan["summary"])

    def test_saved_budget_can_be_replaced_by_unlimited_in_service(self):
        with tempfile.TemporaryDirectory() as directory:
            service = StylingService(engine=self.engine, root=directory)
            try:
                plan = self.engine.plan({"scene": "通勤", "season": "秋季", "budget": 1000})
                service.remember_plan("owner", plan)
                job = service.latest("owner")
                updated = service.refine("owner", job["job_id"], {
                    "feedback": "不以黑色为裤子，不以白色为外套，不以红色为内搭，价格不限"})
                self.assertNotIn("budget", updated["result"]["options"])
                self.assertTrue(updated["result"]["options"]["budget_unlimited"])
                self.assertTrue(updated["result"]["outfits"])
            finally:
                service.close()

    def test_invalid_constraint_shapes_and_values_are_rejected(self):
        for payload in ({"budget_unlimited": "true"}, {"excluded_colors_by_group": []},
                        {"excluded_colors_by_group": {"pants": ["黑"]}},
                        {"excluded_colors_by_group": {"bottoms": ["透明"]}},
                        {"excluded_colors_by_group": {"bottoms": "黑"}}):
            with self.subTest(payload=payload), self.assertRaises(StylingError):
                clean_preferences(payload)


if __name__ == "__main__":
    unittest.main()
