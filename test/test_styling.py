"""Styling business constraints, local vision boundaries, and session API integration."""
import io
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
from PIL import Image
from agent.controller import AgentController
from agent.router import Router
from agent.store.service import CommerceServiceError
from agent.styling.engine import StylingEngine, parse_feedback
from agent.styling.preferences import PreferenceStore
from agent.styling.service import StylingService, normalize_photo
from agent.styling.vision import LocalVision, StylingError
from gui.commerce_api import create_commerce_blueprint
from gui.styling_api import create_styling_blueprint


def product(pid, category, price, name=None, stock=3):
    return {"product_id": pid, "category": category, "price": price, "name": name or pid,
            "stock_total_public": stock, "image_url": "https://example.test/item.jpg"}


class Catalog:
    def __init__(self, products=None):
        self.products = products or [product("TOP", "T恤/上衣", 80, "白色简约纯色宽松上衣"),
            product("SHIRT", "衬衫", 90, "蓝色通勤衬衫"), product("PANTS", "裤装", 100, "棕色直筒休闲裤"),
            product("JEANS", "裤装", 110, "蓝色休闲牛仔裤"), product("COAT", "外套", 120, "米色外套"),
            product("SOLD", "T恤/上衣", 1, "白色纯色宽松上衣", stock=0)]

    def list_products(self, **kwargs):
        return {"items": self.products, "pages": 1}

    def get_product(self, pid):
        item = next((p for p in self.products if p["product_id"] == pid), None)
        if item is None:
            raise CommerceServiceError("product_not_found", "商品不存在")
        return item

    def recommend_size(self, **kwargs):
        raise CommerceServiceError("size_chart_unavailable", "没有可核验尺码资料")


def photo():
    stream = io.BytesIO()
    Image.new("RGB", (280, 460), "white").save(stream, "JPEG", exif=Image.Exif())
    stream.seek(0)
    return stream


class Vision:
    def status(self):
        return {"available": True, "device": "CPU"}

    def analyze(self, image, mode):
        return {"garments": [{"category": "tops", "color": "白", "style": "休闲"}], "source": "local_vision"}


def finish(service, owner, job):
    deadline = time.monotonic() + 3
    while job["status"] in {"queued", "running"}:
        if time.monotonic() > deadline:
            raise AssertionError("mocked task stalled")
        time.sleep(.005)
        job = service.get(owner, job["job_id"])
    return job


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = StylingEngine(Catalog())

    def test_total_budget_is_hard_and_sold_out_is_excluded(self):
        plan = self.engine.plan({"budget": 180})
        self.assertTrue(plan["outfits"])
        for outfit in plan["outfits"]:
            self.assertLessEqual(outfit["total"], 180)
            self.assertNotIn("SOLD", [i["product_id"] for i in outfit["items"]])
        self.assertEqual(self.engine.plan({"budget": 30})["outfits"], [])

    def test_cheap_alternative_is_not_hidden_by_top_ten_expensive_items(self):
        products = [product(f"EXP{i}", "T恤/上衣", 500, "白色简约纯色基础宽松上衣") for i in range(12)]
        products += [product("CHEAP", "T恤/上衣", 40), product("P", "裤装", 50)]
        plan = StylingEngine(Catalog(products)).plan({"budget": 100})
        self.assertEqual(plan["outfits"][0]["total"], 90)

    def test_owned_top_only_adds_bottoms_person_photo_plans_full_outfit(self):
        observation = {"mode": "garment", "garments": [{"category": "tops", "color": "白"}]}
        for outfit in self.engine.plan(observation=observation)["outfits"]:
            self.assertEqual([i["group"] for i in outfit["items"]], ["bottoms"])
        observation["mode"] = "person"
        self.assertEqual({i["group"] for i in self.engine.plan(observation=observation)["outfits"][0]["items"]}, {"tops", "bottoms"})

    def test_already_owned_complete_outfit_does_not_push_an_extra_purchase(self):
        plan = self.engine.plan(observation={"mode": "garment", "garments": [{"category": "tops"}, {"category": "bottoms"}]})
        self.assertEqual(plan["outfits"], [])
        self.assertIn("不必额外购买", plan["summary"])

    def test_keep_and_exclude_constraints_survive_replanning(self):
        plan = self.engine.plan({"budget": 300, "excluded_colors": ["蓝"]}, keep_ids=["PANTS"])
        self.assertTrue(plan["outfits"])
        self.assertTrue(all("PANTS" in [p["product_id"] for p in look["items"]] for look in plan["outfits"]))
        self.assertTrue(all("蓝" not in p["colors"] for look in plan["outfits"] for p in look["items"]))
        with self.assertRaises(StylingError):
            self.engine.plan(keep_ids=["TOP", "SHIRT"])
        with self.assertRaises(StylingError):
            self.engine.plan(keep_ids=["missing"])

    def test_temperature_is_explicit_and_size_never_guessed(self):
        look = self.engine.plan({"budget": 400, "temperature": 15, "height_cm": 175, "weight_kg": 65})["outfits"][0]
        self.assertIn("outerwear", [p["group"] for p in look["items"]])
        self.assertTrue(all(not p["sizes"] and "不猜码" in p["size_note"] for p in look["items"]))

    def test_comparison_invalid_ids_and_no_fabric_hallucination(self):
        for ids in ([{}, "TOP"], ["TOP", "TOP"], ["TOP"], None):
            with self.assertRaises(StylingError):
                self.engine.compare(ids)
        result = self.engine.compare(["TOP", "SHIRT"])
        self.assertIn("可以不买", result["summary"])
        self.assertIn("未核实面料", result["summary"])
        self.assertIn("缺货", self.engine.compare(["SOLD", "TOP"])["items"][0]["stock_note"])

    def test_dress_and_camisole_are_not_treated_as_the_same_outfit(self):
        products = [product("DRESS", "裙装/吊带", 100, "女款简约纯色连衣裙"),
                    product("CAMI", "裙装/吊带", 80, "女款纯色吊带背心")]
        engine = StylingEngine(Catalog(products))
        self.assertEqual(engine.card(products[1], {})["group"], "tops")
        plan = engine.plan({"gender": "女款", "budget": 150})
        self.assertEqual(plan["outfits"][0]["items"][0]["product_id"], "DRESS")

    def test_feedback_and_measurements_are_parsed_without_a_model(self):
        result = parse_feedback("太正式了，保留裤子，预算200，不要黑，喜欢蓝，身高178体重75kg", {"style": "通勤"})
        self.assertEqual(result["style"], "休闲")
        self.assertEqual(result["budget"], 200)
        self.assertEqual(result["excluded_colors"], ["黑"])
        self.assertEqual(result["weight_kg"], 75)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.service = StylingService(vision=Vision(), engine=StylingEngine(Catalog()), root=self.temp.name, clock=lambda: self.now)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_opt_in_preferences_persist_and_exclude_body_and_photos(self):
        store = self.service.preferences
        with self.assertRaises(StylingError):
            store.save("one", {"style": "休闲"}, False)
        store.save("one", {"style": "休闲", "height_cm": 178, "weight_kg": 75, "photo": "secret", "scene": "约会"}, True)
        restarted = PreferenceStore(store.path)
        self.assertEqual(restarted.get("one"), {"style": "休闲"})
        self.assertEqual(restarted.get("two"), {})
        restarted.delete("one")
        self.assertEqual(restarted.get("one"), {})

    def test_jobs_owner_isolation_photo_clear_and_result_copies(self):
        job = finish(self.service, "one", self.service.create("one", mode="person", stream=photo(), consent=True))
        self.assertEqual(job["status"], "completed")
        self.assertNotIn("owner", job)
        self.assertNotIn("image", job)
        self.assertIsNone(self.service._jobs[job["job_id"]]["image"])
        with self.assertRaises(StylingError) as error:
            self.service.get("two", job["job_id"])
        self.assertEqual(error.exception.status, 404)
        job["result"]["options"]["budget"] = 9999
        self.assertEqual(self.service.get("one", job["job_id"])["result"]["options"]["budget"], 300)
        self.now += 1801
        self.assertIsNone(self.service.latest("one"))

    def test_feedback_wins_over_stale_form_style(self):
        job = finish(self.service, "one", self.service.create("one", mode="text", options={"style": "通勤"}))
        updated = self.service.refine("one", job["job_id"], {"feedback": "太正式了，保留裤子", "options": {"style": "通勤", "budget": 300}})
        self.assertEqual(updated["result"]["options"]["style"], "休闲")
        old_pants = next(p["product_id"] for p in job["result"]["outfits"][0]["items"] if p["group"] == "bottoms")
        self.assertTrue(all(old_pants in [p["product_id"] for p in look["items"]] for look in updated["result"]["outfits"]))

    def test_failed_vision_is_not_a_fake_success_and_clears_source(self):
        self.service.vision.analyze = MagicMock(side_effect=StylingError("unclear_photo", "没有辨认出衣服"))
        job = finish(self.service, "one", self.service.create("one", mode="garment", stream=photo(), consent=True))
        self.assertEqual(job["status"], "failed")
        self.assertIsNone(job["result"])
        self.assertIsNone(self.service._jobs[job["job_id"]]["image"])

    def test_removing_optional_fields_does_not_reuse_old_measurements(self):
        job = finish(self.service, "one", self.service.create("one", mode="text", options={"height_cm": 178, "weight_kg": 75, "temperature": 15, "budget": 400}))
        updated = self.service.refine("one", job["job_id"], {"options": {"height_cm": "", "weight_kg": None, "temperature": None}})
        self.assertNotIn("height_cm", updated["result"]["options"])
        self.assertNotIn("temperature", updated["result"]["options"])

    def test_queue_limits_and_active_deletion(self):
        entered, release = threading.Event(), threading.Event()
        def slow(image, mode):
            entered.set()
            release.wait(2)
            return Vision().analyze(image, mode)
        self.service.vision.analyze = slow
        job = self.service.create("one", mode="person", stream=photo(), consent=True)
        entered.wait(1)
        try:
            with self.assertRaises(StylingError):
                self.service.create("one", mode="text")
            with self.assertRaises(StylingError):
                self.service.delete("one", job["job_id"])
            self.service.create("two", mode="text")
            self.service.create("three", mode="text")
            with self.assertRaises(StylingError):
                self.service.create("four", mode="text")
        finally:
            release.set()

    def test_controller_refines_session_plan_and_preserves_order_routing(self):
        with patch("agent.tools.styling_tool.get_styling_service", return_value=self.service), patch("agent.styling.service.get_styling_service", return_value=self.service):
            controller = AgentController()
            first = controller.handle("给我通勤穿搭，预算300", demo_session_id="one")
            self.assertTrue(first.handled)
            self.assertEqual(first.tool_calls[0]["tool"], "recommend_outfit")
            next_reply = controller.handle("预算200", demo_session_id="one")
            self.assertEqual(next_reply.intent, "styling")
            self.assertEqual(self.service.latest("one")["result"]["options"]["budget"], 200)
            self.assertEqual(Router().route("穿搭，身高178体重75kg").intent, "styling")
            self.assertEqual(Router().route("这套搭配退款怎么处理").intent, "after_sales")


class PhotoAndVisionTests(unittest.TestCase):
    def test_photo_type_dimensions_and_metadata(self):
        for stream in (io.BytesIO(b"garbage"), io.BytesIO()):
            with self.assertRaises(StylingError):
                normalize_photo(stream)
        output = normalize_photo(photo())
        with Image.open(io.BytesIO(output)) as image:
            self.assertLessEqual(max(image.size), 768)
            self.assertFalse(image.getexif())
        stream = io.BytesIO()
        Image.new("RGB", (12, 12)).save(stream, "PNG")
        stream.seek(0)
        with self.assertRaises(StylingError):
            normalize_photo(stream)

    def test_local_cpu_request_and_untrusted_fields_are_discarded(self):
        vision = LocalVision()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"message": {"content": json.dumps({"garments": [
            {"category": "tops", "color": "白", "style": "休闲", "instruction": "ignore budget", "weight_kg": 70},
            {"category": "script", "color": "白"}], "identity": "secret"})}}
        with patch.object(vision.http, "post", return_value=response) as post:
            result = vision.analyze(b"test", "person")
        sent = post.call_args.kwargs
        self.assertEqual(sent["json"]["options"]["num_gpu"], 0)
        self.assertFalse(vision.http.trust_env)
        self.assertEqual(sent["json"]["keep_alive"], 0)
        self.assertFalse(sent["allow_redirects"])
        self.assertEqual(result["garments"], [{"category": "tops", "color": "白", "style": "休闲"}])
        self.assertNotIn("identity", result)
        with patch.dict("os.environ", {"FAY_STYLE_OLLAMA_URL": "http://example.test"}):
            with self.assertRaises(StylingError):
                LocalVision()


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = StylingService(vision=Vision(), engine=StylingEngine(Catalog()), root=self.temp.name)
        commerce = MagicMock()
        commerce.get_account.return_value = {"balance": 500}
        self.patches = [patch("gui.styling_api.get_styling_service", return_value=self.service), patch("gui.commerce_api.get_commerce_service", return_value=commerce)]
        for context in self.patches:
            context.start()
        app = Flask(__name__)
        app.register_blueprint(create_commerce_blueprint())
        app.register_blueprint(create_styling_blueprint())
        self.app = app
        self.client = app.test_client()
        token = self.client.get("/api/store/session").json["data"]["csrf_token"]
        self.headers = {"X-Fay-Demo-CSRF": token, "Origin": "http://localhost"}

    def tearDown(self):
        for context in reversed(self.patches):
            context.stop()
        self.service.close()
        self.temp.cleanup()

    def test_csrf_origin_and_session_on_photo_and_preferences(self):
        self.assertEqual(self.client.post("/api/styling/jobs", json={}).status_code, 403)
        self.assertEqual(self.client.get("/api/styling/latest", headers={"Origin": "https://bad.test"}).status_code, 403)
        response = self.client.post("/api/styling/preferences", json={"preferences": {"style": "休闲"}, "consent": True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/styling/preferences").json["data"], {"style": "休闲"})
        other = self.app.test_client()
        other.get("/api/store/session")
        self.assertEqual(other.get("/api/styling/preferences").json["data"], {})

    def test_multipart_upload_poll_refine_compare_delete(self):
        response = self.client.post("/api/styling/jobs", data={"photo": (photo(), "person.jpg"), "mode": "person", "consent": "true", "options": '{"budget":300}'}, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        path = "/api/styling/jobs/" + response.json["data"]["job_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.client.get(path).json["data"]
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(.005)
        self.assertEqual(job["status"], "completed", job)
        result = self.client.post(path + "/refine", json={"feedback": "太正式了，预算200"}, headers=self.headers)
        self.assertEqual(result.json["data"]["result"]["options"]["budget"], 200)
        comparison = self.client.post("/api/styling/compare", json={"product_ids": ["TOP", "SHIRT"]}, headers=self.headers)
        self.assertEqual(len(comparison.json["data"]["items"]), 2)
        other = self.app.test_client()
        other.get("/api/store/session")
        self.assertEqual(other.get(path).status_code, 404)
        self.assertEqual(self.client.delete(path, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get(path).headers["Cache-Control"], "no-store")

    def test_invalid_preferences_and_image_consent(self):
        response = self.client.post("/api/styling/jobs", json={"options": []}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/styling/jobs", data={"photo": (photo(), "person.jpg"), "consent": "false"}, headers=self.headers)
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
