"""Explicit item/color requests must not become loose outfit-wide preferences."""
import tempfile
import unittest
from unittest.mock import patch

from agent.controller import AgentController
from agent.styling.engine import StylingEngine, parse_feedback
from agent.styling.service import StylingService
from test.test_styling import Catalog, product
from test.test_styling_planner import SequencePlanner


QUESTION = "保留上衣，帮我再换一个白色裤子"


class RequiredItemTests(unittest.TestCase):
    def setUp(self):
        self.products = [
            product("TOP", "衬衫", 60, "蓝色通勤衬衫"),
            product("OLD", "裤装", 50, "黑色简约直筒休闲裤"),
            product("WHITE-SKIRT", "裙装/吊带", 20, "白色简约纯色半身裙"),
            product("WHITE-PANTS", "裤装", 80, "白色裤子"),
            product("WHITE-PANTS-ALT", "裤装", 90, "白色阔腿裤"),
            product("BLUE-PANTS", "裤装", 45, "蓝色简约基础直筒裤"),
        ]
        self.engine = StylingEngine(Catalog(self.products))

    def test_reported_sentence_scopes_white_to_trousers(self):
        options = parse_feedback(QUESTION, {"budget": 300, "scene": "通勤"})
        self.assertEqual(options["required_colors_by_group"], {"bottoms": ["白"]})
        self.assertEqual(options["bottoms_kind"], "pants")
        self.assertNotIn("colors", options)

    def test_both_outfits_keep_blue_top_and_have_white_pants(self):
        plan = self.engine.plan(parse_feedback(QUESTION), keep_ids=["TOP"])
        self.assertEqual(len(plan["outfits"]), 2)
        for outfit in plan["outfits"]:
            self.assertIn("TOP", [item["product_id"] for item in outfit["items"]])
            bottoms = [item for item in outfit["items"] if item["group"] == "bottoms"]
            self.assertEqual(len(bottoms), 1)
            self.assertEqual(bottoms[0]["category"], "裤装")
            self.assertIn("白", bottoms[0]["colors"])

    def test_no_white_trousers_does_not_substitute_skirt_or_other_color(self):
        products = [item for item in self.products if not item["product_id"].startswith("WHITE-PANTS")]
        products.append(product("UNKNOWN", "裤装", 15, "简约纯色直筒裤"))
        plan = StylingEngine(Catalog(products)).plan(parse_feedback(QUESTION), keep_ids=["TOP"])
        self.assertFalse(plan["outfits"])
        self.assertIn("未找到", plan["summary"])
        self.assertIn("不会擅自换成其他颜色或款式", plan["summary"])

    def test_sku_colors_are_used_and_unknown_colors_do_not_pass(self):
        pants = product("SKU-WHITE", "裤装", 50, "直筒裤")
        pants["variants"] = [{"attributes": {"颜色": "白色", "尺码": "M"}}]
        plan = StylingEngine(Catalog([self.products[0], pants])).plan(parse_feedback(QUESTION))
        self.assertTrue(plan["outfits"])
        self.assertIn("SKU-WHITE", [item["product_id"] for item in plan["outfits"][0]["items"]])

    def test_required_color_never_relaxes_budget_or_exclusions(self):
        for options in (
                parse_feedback(QUESTION, {"budget": 100}),
                parse_feedback(QUESTION, {"excluded_colors_by_group": {"bottoms": ["白"]}})):
            with self.subTest(options=options):
                plan = self.engine.plan(options, keep_ids=["TOP"])
                self.assertFalse(plan["outfits"])

    def test_explicit_local_color_replaces_previous_color_and_survives_other_feedback(self):
        white = parse_feedback("换一个白色裤子")
        cheaper = parse_feedback("预算250", white)
        self.assertEqual(cheaper["required_colors_by_group"], {"bottoms": ["白"]})
        blue = parse_feedback("裤子换成蓝色", cheaper)
        self.assertEqual(blue["required_colors_by_group"], {"bottoms": ["蓝"]})
        self.assertEqual(blue["bottoms_kind"], "pants")
        self.assertEqual(white["required_colors_by_group"], {"bottoms": ["白"]})

    def test_switching_to_skirt_changes_type_and_dress_releases_old_pants_constraints(self):
        white_pants = parse_feedback("换白色裤子")
        skirt = parse_feedback("换成蓝色半身裙", white_pants)
        self.assertEqual(skirt["bottoms_kind"], "skirt")
        self.assertEqual(skirt["required_colors_by_group"], {"bottoms": ["蓝"]})
        dress = parse_feedback("改成连衣裙", white_pants)
        self.assertNotIn("bottoms_kind", dress)
        self.assertNotIn("bottoms", dress["required_colors_by_group"])

    def test_trouser_wording_and_unrelated_items_keep_color_scope(self):
        for phrase in ("换白色阔腿裤", "裤子颜色改成白色", "换白裤"):
            with self.subTest(phrase=phrase):
                options = parse_feedback(phrase)
                self.assertEqual(options["required_colors_by_group"], {"bottoms": ["白"]})
                self.assertEqual(options["bottoms_kind"], "pants")
        self.assertNotIn("required_colors_by_group", parse_feedback("买白色帽子"))

    def test_negative_color_request_is_not_also_a_positive_request(self):
        for phrase in ("不要白色裤子", "裤子不要白色", "白色裤子不用"):
            with self.subTest(phrase=phrase):
                options = parse_feedback(phrase)
                self.assertNotIn("required_colors_by_group", options)
                self.assertEqual(options["excluded_colors_by_group"], {"bottoms": ["白"]})

    def test_scoped_color_can_be_cleared_without_discarding_pants_request(self):
        options = parse_feedback("裤子颜色不限", parse_feedback("换白色裤子"))
        self.assertEqual(options["required_colors_by_group"], {})
        self.assertEqual(options["bottoms_kind"], "pants")

    def test_skirt_request_never_uses_trousers_or_a_dress(self):
        products = self.products + [product("DRESS", "裙装/吊带", 20, "白色简约连衣裙")]
        plan = StylingEngine(Catalog(products)).plan(parse_feedback("换白色半身裙"), keep_ids=["TOP"])
        self.assertTrue(plan["outfits"])
        self.assertEqual(plan["outfits"][0]["items"][1]["product_id"], "WHITE-SKIRT")

    def test_owned_unknown_bottom_cannot_skip_required_trousers(self):
        observation = {"mode": "garment", "garments": [
            {"category": "tops", "color": "蓝"}, {"category": "bottoms", "color": "白"}]}
        plan = self.engine.plan(parse_feedback("换白色裤子"), observation)
        self.assertTrue(plan["outfits"])
        self.assertEqual(plan["replaced_owned_groups"], ["bottoms"])
        for outfit in plan["outfits"]:
            self.assertEqual(outfit["owned_items"], [{"category": "tops", "color": "蓝"}])
            self.assertEqual(outfit["items"][0]["category"], "裤装")

    def test_actual_controller_keeps_top_and_filters_both_pants_cards(self):
        with tempfile.TemporaryDirectory() as runtime:
            service = StylingService(engine=self.engine, root=runtime)
            try:
                service.remember_plan("test-owner", self.engine.plan(keep_ids=["TOP", "OLD"]))
                planner = SequencePlanner({
                    "action": "recommend_outfit", "updates": {"colors": ["白"]},
                    "evidence": {"colors": "白色裤子"}, "keep_groups": ["tops"],
                    "replace_groups": ["bottoms"],
                    "group_evidence": {"tops": "保留上衣", "bottoms": "换一个白色裤子"},
                })
                with patch("agent.styling.service.get_styling_service", return_value=service), \
                        patch("agent.tools.styling_tool.get_styling_service", return_value=service):
                    response = AgentController(styling_planner=planner).handle(
                        QUESTION, demo_session_id="test-owner")
                self.assertFalse(response.presentation["planner_fallback"])
                plan = response.presentation["plan"]
                self.assertTrue(plan["outfits"])
                for outfit in plan["outfits"]:
                    self.assertIn("TOP", [item["product_id"] for item in outfit["items"]])
                    pants = next(item for item in outfit["items"] if item["group"] == "bottoms")
                    self.assertEqual(pants["category"], "裤装")
                    self.assertIn("白", pants["colors"])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
