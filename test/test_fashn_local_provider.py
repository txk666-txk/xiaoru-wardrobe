from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from agent.tryon import providers
from agent import catalog_images
from agent.tryon.providers import (
    FashnLocalTryOnProvider, FashnTryOnProvider, TryOnProviderError,
    provider_from_environment, resolve_tryon_reference,
)


from test.fixtures.tryon_reference import (
    REFERENCE_PRODUCT_ID, REFERENCE_IMAGE_URL, temporary_reference_catalog,
)


def jpeg_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (320, 480), (210, 205, 198)).save(stream, format="JPEG")
    return stream.getvalue()


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self):
        self.call = None

    def post(self, url, **kwargs):
        self.call = (url, kwargs)
        return Response({"ok": True, "image_base64": base64.b64encode(jpeg_bytes()).decode("ascii")})


class FakeCloudHttp(FakeHttp):
    def post(self, url, **kwargs):
        self.call = (url, kwargs)
        return Response({"id": "test-job"})

    def get(self, url, **kwargs):
        return Response({"status": "completed", "output": [base64.b64encode(jpeg_bytes()).decode("ascii")]})


class FashnLocalProviderTests(unittest.TestCase):
    def setUp(self):
        self.reference_catalog = temporary_reference_catalog()
        self.reference_catalog.__enter__()
        self.addCleanup(self.reference_catalog.__exit__, None, None, None)

    def _local_request(self, garment):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "person.jpg"
            source.write_bytes(jpeg_bytes())
            http = FakeHttp()
            FashnLocalTryOnProvider(http=http).generate(
                person_image=source,
                garment=garment,
                beautify="natural",
                output_path=Path(temp) / "result.jpg",
            )
            return http.call[1]["json"]

    def test_posts_person_and_garment_to_local_service(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "person.jpg"
            output = Path(temp) / "result.jpg"
            source.write_bytes(jpeg_bytes())
            http = FakeHttp()
            provider = FashnLocalTryOnProvider(
                endpoint="http://127.0.0.1:7860/tryon",
                token="local-secret",
                http=http,
                num_timesteps=20,
            )
            provider.generate(
                person_image=source,
                garment={"name": "针织上衣", "category": "上衣", "image_url": "https://example.test/top.jpg"},
                beautify="natural",
                output_path=output,
            )

            self.assertTrue(output.is_file())
            self.assertEqual(http.call[0], "http://127.0.0.1:7860/tryon")
            body = http.call[1]["json"]
            self.assertTrue(body["person_image"].startswith("data:image/jpeg;base64,"))
            self.assertEqual(body["garment_image_url"], "https://example.test/top.jpg")
            self.assertEqual(body["category"], "tops")
            self.assertEqual(body["garment_photo_type"], "model")
            self.assertEqual(body["num_timesteps"], 20)
            self.assertEqual(http.call[1]["headers"]["Authorization"], "Bearer local-secret")
            self.assertEqual(http.call[1]["timeout"], 600)

    def test_environment_provider_uses_shared_default_and_preserves_timeout_override(self):
        with patch.dict("os.environ", {"FAY_TRYON_PROVIDER": "fashn_local"}, clear=True):
            provider = provider_from_environment()
            self.assertEqual(provider.endpoint, "http://127.0.0.1:7860/tryon")
            self.assertEqual(provider.timeout_seconds, 600)
        with patch.dict("os.environ", {"FAY_TRYON_PROVIDER": "fashn_local", "FAY_TRYON_LOCAL_TIMEOUT_SECONDS": "900"}, clear=True):
            self.assertEqual(provider_from_environment().timeout_seconds, 900)

    def test_environment_provider_reads_shared_token_without_changing_constructor(self):
        environ = {"FAY_TRYON_PROVIDER": "fashn_local", "LOCALAPPDATA": r"C:\MockLocalData"}
        with patch.dict("os.environ", environ, clear=True):
            with patch.object(Path, "read_text", return_value="shared-secret\n") as read:
                provider = provider_from_environment()
                self.assertEqual(provider.token, "shared-secret")
                read.assert_called_once_with(encoding="utf-8-sig")
            with patch.object(Path, "read_text", return_value="shared-secret") as read:
                self.assertEqual(FashnLocalTryOnProvider().token, "")
                read.assert_not_called()

    def test_shared_token_uses_local_app_data_path(self):
        
        environ = {"FAY_TRYON_PROVIDER": "fashn_local", "LOCALAPPDATA": r"C:\MockLocalData"}
        with patch.dict("os.environ", environ, clear=True):
            with patch.object(providers, "Path", wraps=Path) as path:
                with patch.object(Path, "read_text", return_value="secret"):
                    self.assertEqual(provider_from_environment().token, "secret")
                path.assert_called_once_with(r"C:\MockLocalData")

    def test_explicit_token_environment_overrides_shared_file_even_when_empty(self):
        for token in ("explicit-secret", ""):
            with self.subTest(token=bool(token)):
                environ = {"FAY_TRYON_PROVIDER": "fashn_local", "FAY_TRYON_LOCAL_TOKEN": token}
                with patch.dict("os.environ", environ, clear=True):
                    with patch.object(Path, "read_text", return_value="shared-secret") as read:
                        self.assertEqual(provider_from_environment().token, token)
                read.assert_not_called()

    def test_missing_empty_or_unreadable_shared_token_keeps_existing_no_token_behavior(self):
        with patch.dict("os.environ", {"FAY_TRYON_PROVIDER": "fashn_local", "LOCALAPPDATA": r"C:\MockLocalData"}, clear=True):
            for error in (FileNotFoundError(), PermissionError(), UnicodeError()):
                with self.subTest(error=type(error).__name__):
                    with patch.object(Path, "read_text", side_effect=error):
                        self.assertEqual(provider_from_environment().token, "")
            with patch.object(Path, "read_text", return_value=" \n"):
                self.assertEqual(provider_from_environment().token, "")

    def test_shared_token_is_never_forwarded_to_unrelated_endpoint(self):
        for url in ("https://example.test/tryon", "http://127.0.0.1:7861/tryon",
                    "http://127.0.0.1:7860/other", "http://user@127.0.0.1:7860/tryon"):
            with self.subTest(url=url):
                environ = {"FAY_TRYON_PROVIDER": "fashn_local", "FAY_TRYON_LOCAL_URL": url}
                with patch.dict("os.environ", environ, clear=True):
                    with patch.object(Path, "read_text", return_value="shared-secret") as read:
                        self.assertEqual(provider_from_environment().token, "")
                read.assert_not_called()

    def test_busy_shared_service_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "person.jpg"
            source.write_bytes(jpeg_bytes())
            response = Response({"ok": False, "error": "model_busy"})
            response.status_code = 429
            http = FakeHttp()
            with patch.object(http, "post", return_value=response):
                with self.assertRaises(TryOnProviderError) as captured:
                    FashnLocalTryOnProvider(http=http).generate(
                        person_image=source, garment={"image_url": "https://example.test/top.jpg"},
                        beautify="natural", output_path=Path(temp) / "result.jpg",
                    )
            self.assertEqual(captured.exception.code, "tryon_provider_busy")
            self.assertIn("稍后重试", str(captured.exception))

    def test_maps_full_length_dress_to_one_piece(self):
        provider = FashnLocalTryOnProvider(http=FakeHttp())
        self.assertEqual(provider._category_for_garment({"category": "连衣裙"}), "one-pieces")
        self.assertEqual(provider._category_for_garment({"category": "半身裙"}), "bottoms")

    def test_known_product_uses_configured_single_garment_reference(self):
        body = self._local_request({
            "product_id": REFERENCE_PRODUCT_ID,
            "image_url": "https://example.test/model-cover.jpg",
            "category": "连衣裙",
        })
        self.assertEqual(body["garment_image_url"], REFERENCE_IMAGE_URL)
        self.assertEqual(body["garment_photo_type"], "flat-lay")
        self.assertEqual(body["category"], "tops")

    def test_explicit_catalog_directory_also_selects_its_tryon_references(self):
        with tempfile.TemporaryDirectory() as directory:
            reference = {
                "product_id": "LOCAL-DEMO-TOP",
                "tryon_image_url": "https://example.test/local-flat-lay.jpg",
                "tryon_category": "tops",
                "tryon_garment_photo_type": "flat-lay",
            }
            (Path(directory) / "tryon_references.json").write_text(
                json.dumps({"references": [reference]}), encoding="utf-8",
            )
            providers._verified_tryon_references.cache_clear()
            try:
                with patch.dict("os.environ", {"FAY_PRODUCT_CATALOG_DIR": directory}):
                    result = resolve_tryon_reference({"product_id": "LOCAL-DEMO-TOP"})
                self.assertEqual(result["image_url"], reference["tryon_image_url"])
                self.assertEqual(result["photo_type"], "flat-lay")
            finally:
                providers._verified_tryon_references.cache_clear()

    def test_missing_catalog_keeps_cover_even_for_previously_configured_product(self):
        with temporary_reference_catalog(configured=False):
            body = self._local_request({
                "product_id": REFERENCE_PRODUCT_ID,
                "image_url": "https://example.test/model-cover.jpg",
                "category": "连衣裙",
            })
            self.assertEqual(body["garment_image_url"], "https://example.test/model-cover.jpg")
            self.assertEqual(body["garment_photo_type"], "model")
            self.assertEqual(body["category"], "one-pieces")
            self.assertIsNone(resolve_tryon_reference({
                "product_id": REFERENCE_PRODUCT_ID,
                "image_url": "https://example.test/model-cover.jpg",
            })["color_label"])

    def test_unknown_product_keeps_cover_and_defaults_to_model_photo(self):
        body = self._local_request({
            "product_id": "UNVERIFIED-PRODUCT",
            "image_url": "https://example.test/model-cover.jpg",
            "category": "半身裙",
        })
        self.assertEqual(body["garment_image_url"], "https://example.test/model-cover.jpg")
        self.assertEqual(body["garment_photo_type"], "model")
        self.assertEqual(body["category"], "bottoms")

    def test_explicit_reference_overrides_verified_default_and_its_metadata(self):
        garment = {
            "product_id": REFERENCE_PRODUCT_ID,
            "image_url": "https://example.test/model-cover.jpg",
            "tryon_image_url": "https://example.test/chosen-reference.jpg",
            "tryon_garment_photo_type": "model",
            "tryon_category": "one-pieces",
            "color_label": "蓝色",
        }
        body = self._local_request(garment)
        self.assertEqual(body["garment_image_url"], "https://example.test/chosen-reference.jpg")
        self.assertEqual(body["garment_photo_type"], "model")
        self.assertEqual(body["category"], "one-pieces")
        self.assertEqual(resolve_tryon_reference(garment)["color_label"], "蓝色")

    def test_explicit_reference_without_color_does_not_inherit_verified_default_color(self):
        reference = resolve_tryon_reference({
            "product_id": REFERENCE_PRODUCT_ID,
            "tryon_image_url": "https://example.test/chosen-reference.jpg",
            "skus": [{"color": "粉色"}],
        })
        self.assertIsNone(reference["color_label"])
        self.assertEqual(reference["image_url"], "https://example.test/chosen-reference.jpg")
        self.assertEqual(reference["photo_type"], "model")

    def test_cloud_provider_uses_same_reference_priority(self):
        cases = [
            ({"product_id": REFERENCE_PRODUCT_ID}, REFERENCE_IMAGE_URL),
            ({"product_id": "UNVERIFIED-PRODUCT"}, "https://example.test/model-cover.jpg"),
            ({"product_id": REFERENCE_PRODUCT_ID, "tryon_image_url": "https://example.test/chosen-reference.jpg"},
             "https://example.test/chosen-reference.jpg"),
        ]
        for fields, expected in cases:
            with self.subTest(product=fields), tempfile.TemporaryDirectory() as temp:
                source = Path(temp) / "person.jpg"
                source.write_bytes(jpeg_bytes())
                http = FakeCloudHttp()
                FashnTryOnProvider("test-key", http=http).generate(
                    person_image=source,
                    garment={"image_url": "https://example.test/model-cover.jpg", **fields},
                    beautify="natural",
                    output_path=Path(temp) / "result.jpg",
                )
                self.assertEqual(http.call[1]["json"]["inputs"]["garment_image"], expected)

    def test_bundled_catalog_photo_is_sent_as_image_data_to_local_and_cloud_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo = root / "catalog-test.jpg"
            photo.write_bytes(jpeg_bytes())
            source = root / "person.jpg"
            source.write_bytes(jpeg_bytes())
            garment = {"image_url": "/static/images/catalog/catalog-test.jpg", "tryon_garment_photo_type": "flat-lay"}
            with patch.object(catalog_images, "CATALOG_IMAGE_DIR", root):
                local_body = self._local_request(garment)
                self.assertNotIn("garment_image_url", local_body)
                self.assertTrue(local_body["garment_image"].startswith("data:image/jpeg;base64,"))
                self.assertEqual(base64.b64decode(local_body["garment_image"].partition(",")[2]), photo.read_bytes())
                http = FakeCloudHttp()
                FashnTryOnProvider("test-key", http=http).generate(
                    person_image=source, garment=garment, beautify="natural", output_path=root / "result.jpg",
                )
                self.assertEqual(http.call[1]["json"]["inputs"]["garment_image"], local_body["garment_image"])

    def test_local_photo_reader_rejects_paths_outside_the_bundled_catalog(self):
        paths = ["/static/images/catalog/../private.jpg", "/static/images/catalog/%2e%2e%2fprivate.jpg",
                 "/static/images/catalog/test.jpg?path=private", "/static/images/catalog/missing.jpg",
                 r"C:\private.jpg", "file:///private.jpg", "http://127.0.0.1/private.jpg"]
        for path in paths:
            with self.subTest(path=path):
                with self.assertRaises(TryOnProviderError) as caught:
                    self._local_request({"image_url": path})
                self.assertEqual(caught.exception.code, "garment_image_unavailable")


if __name__ == "__main__":
    unittest.main()
