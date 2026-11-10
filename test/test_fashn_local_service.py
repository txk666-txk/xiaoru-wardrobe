from __future__ import annotations

import base64
import io
import json
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from PIL import Image

from agent.tryon import local_service


def png_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (16, 24), (60, 100, 180)).save(stream, format="PNG")
    return stream.getvalue()


def data_image() -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes()).decode("ascii")


class ImageResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield from self.chunks


class GarmentDownloadTests(unittest.TestCase):
    url = "https://example.test/garment.png"

    def test_requests_image_is_decoded_without_fallback(self):
        with patch.object(local_service.requests, "get", return_value=ImageResponse([b"", png_bytes()])) as get:
            with patch.object(local_service, "_fetch_garment_windows") as fallback:
                image = local_service._fetch_garment(self.url)
        self.assertEqual(image.size, (16, 24))
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(get.call_args.args, (self.url,))
        self.assertEqual(get.call_args.kwargs["timeout"], 30)
        self.assertTrue(get.call_args.kwargs["stream"])
        self.assertNotEqual(get.call_args.kwargs.get("verify"), False)
        fallback.assert_not_called()

    def test_ssl_error_uses_windows_download_and_decodes_result(self):
        with patch.object(local_service.requests, "get", side_effect=requests.exceptions.SSLError("TLS reset")):
            with patch.object(local_service, "_fetch_garment_windows", return_value=png_bytes()) as fallback:
                image = local_service._fetch_garment(self.url)
        fallback.assert_called_once_with(self.url)
        self.assertEqual(image.size, (16, 24))

    def test_non_https_urls_never_call_network_or_fallback(self):
        for url in ("http://example.test/garment.png", "file:///garment.png", ""):
            with self.subTest(url=url):
                with patch.object(local_service.requests, "get") as get:
                    with patch.object(local_service, "_fetch_garment_windows") as fallback:
                        with self.assertRaisesRegex(local_service.LocalTryOnRequestError, "HTTPS"):
                            local_service._fetch_garment(url)
                get.assert_not_called()
                fallback.assert_not_called()

    def test_oversized_requests_response_is_rejected_without_fallback(self):
        with patch.object(local_service, "MAX_IMAGE_BYTES", 4):
            with patch.object(local_service.requests, "get", return_value=ImageResponse([b"123", b"45"])):
                with patch.object(local_service, "_fetch_garment_windows") as fallback:
                    with self.assertRaisesRegex(local_service.LocalTryOnRequestError, "too large"):
                        local_service._fetch_garment(self.url)
        fallback.assert_not_called()


class WindowsGarmentDownloadTests(unittest.TestCase):
    url = "https://example.test/garment.png"

    def curl_result(self, body: bytes, returncode: int = 0):
        def run(_arguments, **kwargs):
            kwargs["stdout"].write(body)
            return SimpleNamespace(returncode=returncode)

        return run

    def windows_download(self, body: bytes, *, returncode: int = 0):
        with patch.object(local_service.os, "name", "nt"):
            with patch.object(local_service.Path, "is_file", return_value=True):
                with patch.object(local_service.tempfile, "TemporaryFile", return_value=io.BytesIO()):
                    with patch.object(local_service.subprocess, "run", side_effect=self.curl_result(body, returncode)) as run:
                        result = local_service._fetch_garment_windows(self.url)
                        return result, run.call_args

    def test_system_curl_keeps_certificate_https_size_and_timeout_limits(self):
        body = png_bytes()
        result, call = self.windows_download(body)
        self.assertEqual(result, body)
        arguments = call.args[0]
        self.assertEqual(arguments[-1], self.url)
        self.assertIn("--fail", arguments)
        self.assertIn("--location", arguments)
        self.assertEqual(arguments[arguments.index("--proto") + 1], "=https")
        self.assertEqual(arguments[arguments.index("--proto-redir") + 1], "=https")
        self.assertEqual(arguments[arguments.index("--max-time") + 1], "30")
        self.assertEqual(arguments[arguments.index("--max-filesize") + 1], str(local_service.MAX_IMAGE_BYTES))
        self.assertNotIn("--insecure", arguments)
        self.assertNotIn("-k", arguments)
        self.assertEqual(call.kwargs["timeout"], 35)
        self.assertEqual(call.kwargs["stderr"], subprocess.PIPE)

    def test_curl_error_never_accepts_response_body(self):
        with self.assertRaisesRegex(local_service.LocalTryOnRequestError, "could not be downloaded"):
            self.windows_download(png_bytes(), returncode=22)

    def test_curl_success_still_rejects_empty_or_oversized_output(self):
        for body in (b"", b"12345"):
            with self.subTest(body=body):
                with patch.object(local_service, "MAX_IMAGE_BYTES", 4):
                    with self.assertRaisesRegex(local_service.LocalTryOnRequestError, "empty or too large"):
                        self.windows_download(body)

    def test_curl_timeout_is_reported_as_request_error(self):
        with patch.object(local_service.os, "name", "nt"):
            with patch.object(local_service.Path, "is_file", return_value=True):
                with patch.object(local_service.tempfile, "TemporaryFile", return_value=io.BytesIO()):
                    with patch.object(local_service.subprocess, "run", side_effect=subprocess.TimeoutExpired("curl", 35)):
                        with self.assertRaisesRegex(local_service.LocalTryOnRequestError, "could not be downloaded"):
                            local_service._fetch_garment_windows(self.url)

    def test_successful_curl_html_response_is_not_treated_as_image(self):
        with patch.object(local_service.requests, "get", side_effect=requests.exceptions.SSLError("TLS reset")):
            with patch.object(local_service.os, "name", "nt"):
                with patch.object(local_service.Path, "is_file", return_value=True):
                    with patch.object(local_service.tempfile, "TemporaryFile", return_value=io.BytesIO()):
                        with patch.object(local_service.subprocess, "run", side_effect=self.curl_result(b"<html>CDN error</html>")):
                            with self.assertRaisesRegex(local_service.LocalTryOnRequestError, "not a supported image"):
                                local_service._fetch_garment(self.url)


class ImageValidationTests(unittest.TestCase):
    def test_large_image_is_downscaled_and_exif_orientation_applied(self):
        stream = io.BytesIO()
        image = Image.new("RGB", (2400, 1200), (20, 90, 140))
        exif = Image.Exif()
        exif[274] = 6
        image.save(stream, format="JPEG", exif=exif)
        encoded = base64.b64encode(stream.getvalue()).decode("ascii")
        result = local_service._decode_data_image(encoded, field="person_image")
        self.assertEqual(result.size, (1024, 2048))
        self.assertEqual(result.mode, "RGB")
        self.assertFalse(result.info)

    def test_invalid_json_image_values_are_client_errors(self):
        for value in (None, {}, "", "data:image/jpeg;base64,@@", "data:image/jpeg"):
            with self.subTest(value=value), self.assertRaises(local_service.LocalTryOnRequestError):
                local_service._decode_data_image(value, field="garment_image")

    def test_pixel_limit_rejected_before_decoding(self):
        with patch.object(local_service, "MAX_IMAGE_PIXELS", 4):
            with self.assertRaisesRegex(local_service.LocalTryOnRequestError, "too many pixels"):
                local_service._decode_data_image(data_image(), field="person_image")


class FakeRuntime:
    def __init__(self, error=None):
        self._pipeline = None
        self._load_error = None
        self.calls = []
        self.error = error

    def generate(self, **options):
        self.calls.append(options)
        if self.error is not None:
            raise self.error
        return base64.b64encode(png_bytes()).decode("ascii")


class SharedServiceHttpTests(unittest.TestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.server = local_service.FashnVtonServer(("127.0.0.1", 0), self.runtime, "test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.headers = {"Authorization": "Bearer test-token"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def post(self, payload, *, authorized=True):
        return requests.post(
            self.base_url + "/tryon", json=payload,
            headers=self.headers if authorized else {}, timeout=5,
        )

    def test_health_advertises_shared_protocol_without_loading_model(self):
        response = requests.get(self.base_url + "/health", timeout=5)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["service"], "fashn-vton-1.5")
        self.assertEqual(payload["protocol_version"], 2)
        self.assertEqual(payload["accepted_inputs"], ["garment_image", "garment_image_url"])
        self.assertTrue(payload["serialized_inference"])
        self.assertFalse(payload["pipeline_loaded"])
        self.assertFalse(payload["initialization_failed"])
        self.assertTrue(payload["auth_required"])
        self.assertFalse(self.runtime.calls)

    def test_authenticated_health_checks_shared_token(self):
        for headers, expected in (({}, 401), ({"Authorization": "Bearer wrong"}, 401), (self.headers, 200)):
            with self.subTest(headers=headers):
                response = requests.get(self.base_url + "/health/auth", headers=headers, timeout=5)
                self.assertEqual(response.status_code, expected)
        self.runtime._load_error = RuntimeError("load failed")
        response = requests.get(self.base_url + "/health/auth", headers=self.headers, timeout=5)
        self.assertTrue(response.json()["initialization_failed"])
        self.assertFalse(self.runtime.calls)

    def test_wardrobe_base64_input_does_not_fetch_network(self):
        with patch.object(local_service, "_fetch_garment") as fetch:
            response = self.post({"person_image": data_image(), "garment_image": data_image()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["image_base64"])
        self.assertEqual(self.runtime.calls[0]["category"], "tops")
        self.assertEqual(self.runtime.calls[0]["garment_photo_type"], "flat-lay")
        self.assertEqual(self.runtime.calls[0]["num_timesteps"], 20)
        fetch.assert_not_called()
        # Request images are closed after generation, including success paths.
        with self.assertRaises(ValueError):
            self.runtime.calls[0]["person_image"].getpixel((0, 0))
        with self.assertRaises(ValueError):
            self.runtime.calls[0]["garment_image"].getpixel((0, 0))

    def test_fay_url_input_remains_supported(self):
        url = "https://example.test/garment.png"
        with patch.object(local_service, "_fetch_garment", return_value=Image.new("RGB", (16, 24))) as fetch:
            response = self.post({"person_image": data_image(), "garment_image_url": url,
                                  "category": "one-pieces", "garment_photo_type": "model"})
        self.assertEqual(response.status_code, 200, response.text)
        fetch.assert_called_once_with(url)
        self.assertEqual(self.runtime.calls[0]["category"], "one-pieces")

    def test_garment_fields_are_mutually_exclusive(self):
        cases = ({}, {"garment_image": data_image(), "garment_image_url": "https://example.test/image.jpg"},
                 {"garment_image": None}, {"garment_image_url": ""})
        for fields in cases:
            with self.subTest(fields=list(fields)):
                response = self.post({"person_image": data_image(), **fields})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"], "invalid_request")
        self.assertFalse(self.runtime.calls)

    def test_missing_auth_is_rejected_before_inference(self):
        response = self.post({"person_image": data_image(), "garment_image": data_image()}, authorized=False)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.runtime.calls)

    def test_malformed_json_is_a_client_error(self):
        response = requests.post(self.base_url + "/tryon", data=b"{", headers=self.headers, timeout=5)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_request")

    def test_invalid_sampling_options_are_client_errors(self):
        cases = ({"category": "shoes"}, {"garment_photo_type": "unknown"},
                 {"num_timesteps": True}, {"num_timesteps": 0}, {"num_timesteps": "not-a-number"})
        for options in cases:
            with self.subTest(options=options):
                response = self.post({"person_image": data_image(), "garment_image": data_image(), **options})
                self.assertEqual(response.status_code, 400)
        self.assertFalse(self.runtime.calls)

    def test_busy_service_returns_retryable_429(self):
        self.runtime.error = local_service.ModelBusyError("busy")
        response = self.post({"person_image": data_image(), "garment_image": data_image()})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"], "model_busy")
        with self.assertRaises(ValueError):
            self.runtime.calls[0]["person_image"].getpixel((0, 0))

    def test_no_auth_configuration_keeps_loopback_development_supported(self):
        self.server.auth_token = ""
        response = requests.get(self.base_url + "/health/auth", timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["auth_required"])


class SharedRuntimeTests(unittest.TestCase):
    def generate(self, runtime):
        return runtime.generate(
            person_image=Image.new("RGB", (16, 24)), garment_image=Image.new("RGB", (16, 24)),
            category="tops", garment_photo_type="flat-lay", num_timesteps=20,
        )

    def test_concurrent_requests_load_once_and_serialize_inference(self):
        runtime = local_service.FashnVtonRuntime(Path("weights"))
        first_entered = threading.Event()
        second_started = threading.Event()
        release = threading.Event()
        active = 0
        peak_active = 0
        calls = 0

        def pipeline(**_options):
            nonlocal active, peak_active, calls
            active += 1
            peak_active = max(peak_active, active)
            calls += 1
            try:
                if calls == 1:
                    first_entered.set()
                    if not release.wait(timeout=5):
                        raise RuntimeError("Test did not release inference")
                return SimpleNamespace(images=[Image.new("RGB", (16, 24))])
            finally:
                active -= 1

        factory = unittest.mock.Mock(return_value=pipeline)

        def second_request():
            second_started.set()
            return self.generate(runtime)

        with patch.dict("sys.modules", {"fashn_vton": SimpleNamespace(TryOnPipeline=factory)}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.generate, runtime)
                self.assertTrue(first_entered.wait(timeout=5))
                second = pool.submit(second_request)
                self.assertTrue(second_started.wait(timeout=5))
                self.assertFalse(second.done())
                release.set()
                self.assertTrue(first.result(timeout=5))
                self.assertTrue(second.result(timeout=5))
        factory.assert_called_once_with(weights_dir="weights", device=None)
        self.assertEqual(calls, 2)
        self.assertEqual(peak_active, 1)

    def test_failed_initialization_is_cached_and_releases_queue_slot(self):
        runtime = local_service.FashnVtonRuntime(Path("weights"))
        factory = unittest.mock.Mock(side_effect=RuntimeError("Invalid weights"))
        with patch.dict("sys.modules", {"fashn_vton": SimpleNamespace(TryOnPipeline=factory)}):
            for _ in range(6):
                with self.assertRaisesRegex(RuntimeError, "failed to initialize"):
                    self.generate(runtime)
        self.assertEqual(factory.call_count, 1)
        self.assertIsNotNone(runtime._load_error)

    def test_queue_rejects_fifth_request_and_recovers(self):
        runtime = local_service.FashnVtonRuntime(Path("weights"))
        runtime._pipeline = lambda **_: SimpleNamespace(images=[Image.new("RGB", (16, 24))])
        for _ in range(4):
            self.assertTrue(runtime._slots.acquire(blocking=False))
        try:
            with self.assertRaises(local_service.ModelBusyError):
                self.generate(runtime)
        finally:
            for _ in range(4):
                runtime._slots.release()
        self.assertTrue(self.generate(runtime))

    def test_binding_rejects_non_loopback_addresses(self):
        for host in ("0.0.0.0", "192.168.1.2", "example.test"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "loopback"):
                local_service.FashnVtonServer((host, 0), FakeRuntime())
        self.assertEqual(local_service._loopback_host("localhost"), "127.0.0.1")
        self.assertEqual(local_service._loopback_host("::1"), "::1")
        self.assertEqual(local_service.MAX_REQUEST_BYTES, 48 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
