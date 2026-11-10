from __future__ import annotations

import base64
import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from PIL import Image

from agent.tryon.providers import (
    DisabledTryOnProvider,
    FashnLocalTryOnProvider,
    FashnTryOnProvider,
    TryOnProvider,
)
from agent.tryon.service import MAX_UPLOAD_BYTES, MAX_NORMALIZED_IMAGE_SIDE, RESULT_TTL_SECONDS, TryOnError, TryOnService
from gui.commerce_api import COOKIE_NAME, _csrf_token
from gui.tryon_api import MAX_TRYON_REQUEST_BYTES, create_tryon_blueprint


from test.fixtures.tryon_reference import (
    REFERENCE_PRODUCT_ID, REFERENCE_IMAGE_URL, temporary_reference_catalog,
)


def jpeg_bytes(width: int = 480, height: int = 720) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (width, height), (210, 205, 198)).save(stream, format="JPEG")
    return stream.getvalue()


class FakeCommerceService:
    def __init__(self) -> None:
        self.repository = self
        self.sessions: set[str] = set()
        self.products = {
            "DRESS-1": {
                "product_id": "DRESS-1",
                "name": "测试连衣裙",
                "category": "裙装/吊带",
                "image_url": "https://example.test/dress.jpg",
            },
            "CUP-1": {
                "product_id": "CUP-1",
                "name": "测试水杯",
                "category": "家居",
                "image_url": "https://example.test/cup.jpg",
            },
            REFERENCE_PRODUCT_ID: {
                "product_id": REFERENCE_PRODUCT_ID,
                "name": "测试上衣",
                "category": "上衣",
                "image_url": "https://example.test/blue-cover.jpg",
                "skus": [{"color": "蓝色"}, {"color": "粉色"}],
            },
        }

    def ensure_session(self, session_id: str) -> None:
        self.sessions.add(session_id)

    def get_product(self, product_id: str):
        product = self.products.get(product_id)
        if product is None:
            error = ValueError("not found")
            error.code = "product_not_found"
            raise error
        return dict(product)


class CopyProvider(TryOnProvider):
    name = "copy-test"

    def generate(self, *, person_image: Path, garment, beautify: str, output_path: Path) -> None:
        self.garment = dict(garment)
        del beautify
        output_path.write_bytes(person_image.read_bytes())


class TryOnServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reference_catalog = temporary_reference_catalog()
        self.reference_catalog.__enter__()
        self.addCleanup(self.reference_catalog.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory()
        self.commerce = FakeCommerceService()
        self.service = TryOnService(
            provider=CopyProvider(),
            commerce_service=self.commerce,
            runtime_dir=Path(self.temp.name),
        )

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def test_idle_reaper_removes_expired_results_without_another_request(self):
        self.service.close()
        now = [100000.0]
        self.service = TryOnService(provider=CopyProvider(), commerce_service=self.commerce,
                                    runtime_dir=Path(self.temp.name), clock=lambda: now[0], cleanup_interval=0.02)
        result = self.create()
        path = self.service.get_job('session-a', result['job_id']).result_path
        now[0] += RESULT_TTL_SECONDS + 1
        deadline = time.monotonic() + 2
        while path.exists() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertFalse(path.exists())
        self.service.close()
        self.assertFalse(self.service._reaper.is_alive())

    def test_active_generation_is_not_deleted_by_cleanup(self):
        def generate(**kwargs):
            future = self.service.clock() + RESULT_TTL_SECONDS + 10
            with patch.object(self.service, 'clock', return_value=future):
                self.service._cleanup_expired()
            kwargs['output_path'].write_bytes(kwargs['person_image'].read_bytes())
        with patch.object(self.service.provider, 'generate', side_effect=generate):
            result = self.create()
        self.assertTrue(self.service.get_job('session-a', result['job_id']).result_path.exists())

    def test_expired_locked_result_is_inaccessible_and_deletion_is_retried(self):
        result = self.create()
        job = self.service.get_job('session-a', result['job_id'])
        job.expires_at = self.service.clock() - 1
        with patch('agent.tryon.service.shutil.rmtree'):
            with self.assertRaises(TryOnError):
                self.service.get_job('session-a', result['job_id'])
        self.assertTrue(job.result_path.exists())
        self.service._cleanup_expired()
        self.assertFalse(job.result_path.exists())

    def test_failed_source_removal_does_not_leave_a_permanently_protected_job(self):
        with patch.object(Path, 'unlink', side_effect=PermissionError('photo locked')):
            with self.assertRaises(PermissionError):
                self.create()
        self.assertFalse(self.service._active_jobs)
        self.assertFalse(self.service._jobs)

    def test_startup_cleanup_removes_expired_orphan_result(self):
        self.service.close()
        orphan = Path(self.temp.name) / 'previous-session' / 'previous-job'
        orphan.mkdir(parents=True)
        (orphan / 'result.jpg').write_bytes(jpeg_bytes())
        previous = time.time() - RESULT_TTL_SECONDS - 1
        os.utime(orphan, (previous, previous))
        self.service = TryOnService(provider=CopyProvider(), commerce_service=self.commerce,
                                    runtime_dir=Path(self.temp.name))
        self.assertFalse(orphan.exists())

    def create(self, session_id: str = "session-a"):
        return self.service.create(
            session_id=session_id,
            product_id="DRESS-1",
            beautify="natural",
            consent=True,
            upload_stream=io.BytesIO(jpeg_bytes()),
        )

    def test_creates_session_scoped_result_and_removes_source(self):
        result = self.create()
        job = self.service.get_job("session-a", result["job_id"])
        self.assertTrue(job.result_path.is_file())
        self.assertFalse((job.result_path.parent / "source.jpg").exists())
        with self.assertRaises(TryOnError) as caught:
            self.service.get_job("session-b", result["job_id"])
        self.assertEqual(caught.exception.code, "tryon_result_not_found")

    def test_verified_reference_metadata_matches_actual_local_provider_request(self):
        http = MockLocalHttp()
        self.service.provider = FashnLocalTryOnProvider(http=http)
        reference = self.service.status(REFERENCE_PRODUCT_ID)["reference"]
        result = self.service.create(
            session_id="session-a",
            product_id=REFERENCE_PRODUCT_ID,
            beautify="natural",
            consent=True,
            upload_stream=io.BytesIO(jpeg_bytes()),
        )
        self.assertEqual(reference, {
            "image_url": REFERENCE_IMAGE_URL,
            "color_label": "粉色",
            "category": "tops",
            "photo_type": "flat-lay",
        })
        self.assertEqual(result["reference"], reference)
        body = http.post_call[1]["json"]
        self.assertEqual(body["garment_image_url"], reference["image_url"])
        self.assertEqual(body["category"], reference["category"])
        self.assertEqual(body["garment_photo_type"], reference["photo_type"])
        self.assertEqual(self.commerce.products[REFERENCE_PRODUCT_ID]["image_url"], "https://example.test/blue-cover.jpg")
        self.assertNotIn("color_label", self.commerce.products[REFERENCE_PRODUCT_ID])

    def test_unknown_reference_does_not_infer_color_from_name_or_skus(self):
        self.commerce.products["DRESS-1"].update({
            "name": "黑色测试连衣裙",
            "skus": [{"color": "白色"}],
        })
        reference = self.service.status("DRESS-1")["reference"]
        result = self.create()
        self.assertIsNone(reference["color_label"])
        self.assertEqual(reference["image_url"], "https://example.test/dress.jpg")
        self.assertEqual(reference["photo_type"], "model")
        self.assertEqual(result["reference"], reference)
        self.assertEqual(self.service.provider.garment["image_url"], reference["image_url"])
        self.assertEqual(self.service.provider.garment["tryon_image_url"], reference["image_url"])
        self.assertIsNone(self.service.provider.garment["color_label"])

    def test_delete_removes_result_directory(self):
        result = self.create()
        job = self.service.get_job("session-a", result["job_id"])
        directory = job.result_path.parent
        self.service.delete("session-a", result["job_id"])
        self.assertFalse(directory.exists())

    def test_upload_size_limit_accepts_exactly_64mb_and_rejects_next_byte(self):
        self.assertEqual(MAX_UPLOAD_BYTES, 64 * 1024 * 1024)
        self.assertEqual(self.service.status()["max_upload_mb"], 64)
        with io.BytesIO(b"x" * MAX_UPLOAD_BYTES) as stream:
            self.assertEqual(len(self.service._read_upload(stream)), MAX_UPLOAD_BYTES)
        with patch.object(self.service.provider, "generate") as generate:
            with io.BytesIO(b"x" * (MAX_UPLOAD_BYTES + 1)) as stream:
                with self.assertRaises(TryOnError) as caught:
                    self.service.create(
                        session_id="session-a",
                        product_id="DRESS-1",
                        beautify="natural",
                        consent=True,
                        upload_stream=stream,
                    )
            self.assertEqual(caught.exception.code, "image_too_large")
            self.assertEqual(caught.exception.status, 413)
            self.assertIn("64MB", str(caught.exception))
            generate.assert_not_called()
        self.assertFalse(any(Path(self.temp.name).rglob("source.jpg")))

    def test_larger_upload_limit_keeps_pixel_protection_before_provider(self):
        stream = io.BytesIO()
        Image.new("1", (6001, 4000)).save(stream, format="PNG")
        stream.seek(0)
        with patch.object(self.service.provider, "generate") as generate:
            with self.assertRaises(TryOnError) as caught:
                self.service.create(
                    session_id="session-a",
                    product_id="DRESS-1",
                    beautify="natural",
                    consent=True,
                    upload_stream=stream,
                )
            self.assertEqual(caught.exception.status, 413)
            self.assertIn("像素", str(caught.exception))
            generate.assert_not_called()

    def test_rejects_unverified_non_apparel_and_bad_image(self):
        with self.assertRaises(TryOnError) as caught:
            self.service.create(
                session_id="session-a",
                product_id="CUP-1",
                beautify="natural",
                consent=True,
                upload_stream=io.BytesIO(jpeg_bytes()),
            )
        self.assertEqual(caught.exception.code, "product_not_apparel")
        with self.assertRaises(TryOnError) as caught:
            self.service.create(
                session_id="session-a",
                product_id="DRESS-1",
                beautify="natural",
                consent=True,
                upload_stream=io.BytesIO(b"not-an-image"),
            )
        self.assertEqual(caught.exception.code, "invalid_image")

    def test_requires_consent_and_configured_provider(self):
        with self.assertRaises(TryOnError) as caught:
            self.service.create(
                session_id="session-a",
                product_id="DRESS-1",
                beautify="natural",
                consent=False,
                upload_stream=io.BytesIO(jpeg_bytes()),
            )
        self.assertEqual(caught.exception.code, "privacy_consent_required")
        disabled = TryOnService(
            provider=DisabledTryOnProvider(),
            commerce_service=self.commerce,
            runtime_dir=Path(self.temp.name),
        )
        self.addCleanup(disabled.close)
        with self.assertRaises(TryOnError) as caught:
            disabled.create(
                session_id="session-a",
                product_id="DRESS-1",
                beautify="natural",
                consent=True,
                upload_stream=io.BytesIO(jpeg_bytes()),
            )
        self.assertEqual(caught.exception.code, "tryon_provider_not_configured")
        self.assertFalse(any(Path(self.temp.name).rglob("source.jpg")))


class MockResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class MockLocalHttp:
    def __init__(self):
        self.post_call = None

    def post(self, url, **kwargs):
        self.post_call = (url, kwargs)
        return MockResponse({"image_base64": base64.b64encode(jpeg_bytes()).decode("ascii")})


class MockFashnHttp:
    def __init__(self, result: bytes) -> None:
        self.result = result
        self.post_call = None
        self.polls = 0

    def post(self, url, **kwargs):
        self.post_call = (url, kwargs)
        return MockResponse({"id": "remote-job"})

    def get(self, url, **kwargs):
        del url, kwargs
        self.polls += 1
        return MockResponse({"status": "completed", "output": [base64.b64encode(self.result).decode("ascii")]})


class FashnProviderTests(unittest.TestCase):
    def test_uses_data_uri_and_base64_result_without_exposing_key(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "person.jpg"
            output = Path(temp) / "result.jpg"
            source.write_bytes(jpeg_bytes())
            http = MockFashnHttp(jpeg_bytes())
            provider = FashnTryOnProvider("secret-key", http=http, poll_interval=0)
            provider.generate(
                person_image=source,
                garment={"image_url": "https://example.test/dress.jpg"},
                beautify="light",
                output_path=output,
            )
            self.assertTrue(output.is_file())
            body = http.post_call[1]["json"]
            self.assertEqual(body["model_name"], "tryon-v1.6")
            self.assertTrue(body["inputs"]["model_image"].startswith("data:image/jpeg;base64,"))
            self.assertTrue(body["inputs"]["return_base64"])
            self.assertNotIn("secret-key", str(body))


class TryOnApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reference_catalog = temporary_reference_catalog()
        self.reference_catalog.__enter__()
        self.addCleanup(self.reference_catalog.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory()
        self.commerce = FakeCommerceService()
        self.service = TryOnService(
            provider=CopyProvider(),
            commerce_service=self.commerce,
            runtime_dir=Path(self.temp.name),
        )
        self.app = Flask(__name__)
        self.app.register_blueprint(create_tryon_blueprint())
        self.client = self.app.test_client()
        self.service_patch = patch("gui.tryon_api.get_tryon_service", return_value=self.service)
        self.commerce_patch = patch("gui.commerce_api.get_commerce_service", return_value=self.commerce)
        self.service_patch.start()
        self.commerce_patch.start()

    def tearDown(self) -> None:
        self.service_patch.stop()
        self.commerce_patch.stop()
        self.service.close()
        self.temp.cleanup()

    def session_headers(self):
        response = self.client.get("/api/tryon/status")
        cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        raw = cookie.split("=", 1)[1]
        self.assertTrue(cookie.startswith(f"{COOKIE_NAME}="))
        return {"X-Fay-Demo-CSRF": _csrf_token(raw), "Origin": "http://localhost"}

    def test_upload_result_download_and_delete(self):
        headers = self.session_headers()
        response = self.client.post(
            "/api/tryon/jobs",
            headers=headers,
            data={
                "product_id": "DRESS-1",
                "beautify": "natural",
                "consent": "true",
                "photo": (io.BytesIO(jpeg_bytes()), "person.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["data"]["reference"], self.service.status("DRESS-1")["reference"])
        job_id = response.get_json()["data"]["job_id"]
        image = self.client.get(f"/api/tryon/jobs/{job_id}/image", headers={"Origin": "http://localhost"})
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["Cache-Control"], "private, no-store, max-age=0")
        image.close()
        download_url = f"/api/tryon/jobs/{job_id}/image?download=1"
        attachment = self.client.get(download_url)
        self.assertEqual(attachment.status_code, 200)
        self.assertEqual(attachment.mimetype, "image/jpeg")
        self.assertEqual(attachment.headers["Content-Disposition"], "attachment; filename=fay-tryon-DRESS-1.jpg")
        self.assertEqual(attachment.headers["Cache-Control"], "private, no-store, max-age=0")
        self.assertEqual(attachment.headers["X-Content-Type-Options"], "nosniff")
        with Image.open(io.BytesIO(attachment.data)) as downloaded:
            self.assertEqual(downloaded.format, "JPEG")
            self.assertEqual(downloaded.size, (480, 720))
            downloaded.verify()
        attachment.close()
        other_session = self.app.test_client()
        unauthorized = other_session.get(download_url)
        self.assertEqual(unauthorized.status_code, 404)
        self.assertEqual(unauthorized.get_json()["error"], "tryon_result_not_found")
        deleted = self.client.delete(f"/api/tryon/jobs/{job_id}", headers=headers)
        self.assertEqual(deleted.status_code, 200)
        missing = self.client.get(f"/api/tryon/jobs/{job_id}/image", headers={"Origin": "http://localhost"})
        self.assertEqual(missing.status_code, 404)
        missing_download = self.client.get(download_url)
        self.assertEqual(missing_download.status_code, 404)

    def test_download_at_expiry_returns_json_404_and_removes_file(self):
        now = self.service.clock()
        self.service.clock = lambda: now
        response = self.client.post(
            "/api/tryon/jobs",
            headers=self.session_headers(),
            data={
                "product_id": "DRESS-1",
                "consent": "true",
                "photo": (io.BytesIO(jpeg_bytes()), "person.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        job_id = response.get_json()["data"]["job_id"]
        self.assertTrue(any(Path(self.temp.name).rglob("result.jpg")))
        now += RESULT_TTL_SECONDS
        attachment = self.client.get(f"/api/tryon/jobs/{job_id}/image?download=1")
        self.assertEqual(attachment.status_code, 404)
        self.assertEqual(attachment.mimetype, "application/json")
        self.assertEqual(attachment.get_json()["error"], "tryon_result_not_found")
        self.assertNotIn("Content-Disposition", attachment.headers)
        self.assertFalse(any(Path(self.temp.name).rglob("result.jpg")))

    def test_status_optional_product_previews_actual_reference_without_changing_default_response(self):
        default_response = self.client.get("/api/tryon/status").get_json()["data"]
        self.assertNotIn("reference", default_response)
        response = self.client.get(f"/api/tryon/status?product_id={REFERENCE_PRODUCT_ID}")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        reference = data.pop("reference")
        self.assertEqual(data, default_response)
        self.assertEqual(reference["image_url"], REFERENCE_IMAGE_URL)
        self.assertEqual(reference["color_label"], "粉色")
        self.assertEqual(reference["photo_type"], "flat-lay")
        unknown = self.client.get("/api/tryon/status?product_id=MISSING-PRODUCT")
        self.assertEqual(unknown.status_code, 404)

    def test_valid_photo_over_old_8mb_limit_is_scaled_before_local_provider(self):
        # 实际 RGB PNG 超过旧上限，不用伪造文件长度或绕过图片解码。
        image_bytes = io.BytesIO()
        Image.frombytes("RGB", (2200, 1600), os.urandom(2200 * 1600 * 3)).save(image_bytes, format="PNG")
        self.assertGreater(image_bytes.tell(), 8 * 1024 * 1024)
        image_bytes.seek(0)
        http = MockLocalHttp()
        self.service.provider = FashnLocalTryOnProvider(http=http)
        response = self.client.post(
            "/api/tryon/jobs",
            headers=self.session_headers(),
            data={
                "product_id": "DRESS-1",
                "beautify": "natural",
                "consent": "true",
                "photo": (image_bytes, "large-person.png"),
            },
            content_type="multipart/form-data",
        )
        self.addCleanup(response.request.input_stream.close)
        self.assertEqual(response.status_code, 201, response.get_json())
        encoded = http.post_call[1]["json"]["person_image"].partition(",")[2]
        normalized_bytes = base64.b64decode(encoded)
        self.assertLess(len(normalized_bytes), 16 * 1024 * 1024)
        with Image.open(io.BytesIO(normalized_bytes)) as normalized:
            self.assertEqual(max(normalized.size), MAX_NORMALIZED_IMAGE_SIDE)
            self.assertAlmostEqual(normalized.width / normalized.height, 2200 / 1600, places=3)
        self.assertFalse(any(Path(self.temp.name).rglob("source.jpg")))

    def test_oversized_http_request_returns_json_413_before_provider(self):
        headers = self.session_headers()
        with patch.object(self.service.provider, "generate") as generate:
            response = self.client.post(
                "/api/tryon/jobs",
                headers=headers,
                data=b"",
                content_type="multipart/form-data; boundary=test-boundary",
                environ_overrides={"CONTENT_LENGTH": str(MAX_TRYON_REQUEST_BYTES + 1)},
            )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.get_json()["error"], "image_too_large")
            self.assertIn("64MB", response.get_json()["message"])
            generate.assert_not_called()

    def test_file_over_64mb_returns_json_413_with_valid_multipart(self):
        with patch.object(self.service.provider, "generate") as generate:
            response = self.client.post(
                "/api/tryon/jobs",
                headers=self.session_headers(),
                data={
                    "product_id": "DRESS-1",
                    "consent": "true",
                    "photo": (io.BytesIO(b"x" * (MAX_UPLOAD_BYTES + 1)), "oversize.jpg"),
                },
                content_type="multipart/form-data",
            )
            self.addCleanup(response.request.input_stream.close)
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.get_json()["error"], "image_too_large")
            self.assertIn("64MB", response.get_json()["message"])
            generate.assert_not_called()

    def test_upload_requires_csrf(self):
        self.client.get("/api/tryon/status")
        response = self.client.post(
            "/api/tryon/jobs",
            data={"photo": (io.BytesIO(jpeg_bytes()), "person.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
