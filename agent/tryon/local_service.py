"""Minimal HTTP service for the local FASHN VTON v1.5 pipeline.

Run this module with the *separate* FASHN environment, where ``fashn-vton``
and its CUDA dependencies are installed.  Fay itself calls ``POST /tryon``
and receives a short-lived base64 encoded JPEG; no model weights are loaded
in the Fay process.

Example (PowerShell, from this project's root after installing FASHN):

    .. code-block:: powershell

       & $env:FAY_FASHN_VTON_PYTHON -m agent.tryon.local_service `
         --weights-dir $env:FAY_FASHN_VTON_WEIGHTS_DIR

The endpoint accepts a JSON body with ``person_image`` (data URI), exactly one
of ``garment_image`` (base64/data URI) or ``garment_image_url`` (HTTPS URL),
``category`` and optional sampling options. Fay and the wardrobe app share
this service; loading and inference use the same lock.
It intentionally accepts no arbitrary local file paths.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import io
import ipaddress
import json
import logging
import os
import socket
import subprocess
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, RLock
from typing import Any

import requests
from PIL import Image, ImageOps, UnidentifiedImageError


LOGGER = logging.getLogger("fay.fashn_local_service")
MAX_REQUEST_BYTES = 48 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_IMAGE_SIDE = 2048
VALID_CATEGORIES = {"tops", "bottoms", "one-pieces"}
VALID_PHOTO_TYPES = {"model", "flat-lay"}


class LocalTryOnRequestError(ValueError):
    """A client-side request validation error."""


class ModelBusyError(RuntimeError):
    """The shared service already has one running and three waiting requests."""


def _decode_data_image(value: Any, *, field: str) -> Image.Image:
    if not isinstance(value, str):
        raise LocalTryOnRequestError(f"{field} must be base64 or an image data URI")
    encoded = value.strip()
    if encoded.startswith("data:"):
        _, separator, encoded = encoded.partition(",")
        if not separator:
            raise LocalTryOnRequestError(f"{field} is not a valid data URI")
    if not encoded:
        raise LocalTryOnRequestError(f"{field} is required")
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise LocalTryOnRequestError(f"{field} is too large")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise LocalTryOnRequestError(f"{field} is not valid base64") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise LocalTryOnRequestError(f"{field} is too large")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise LocalTryOnRequestError(f"{field} has too many pixels")
            source.verify()
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
            image.info.clear()
            return image
    except LocalTryOnRequestError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise LocalTryOnRequestError(f"{field} is not a supported image") from exc


def _fetch_garment(url: Any) -> Image.Image:
    garment_url = str(url or "").strip()
    if not garment_url.startswith("https://"):
        raise LocalTryOnRequestError("garment_image_url must be an HTTPS URL")
    try:
        response = requests.get(
            garment_url,
            headers={"User-Agent": "Fay-local-tryon/1.0"},
            timeout=30,
            stream=True,
        )
        with response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise LocalTryOnRequestError("garment image is too large")
                chunks.append(chunk)
        raw = b"".join(chunks)
    except LocalTryOnRequestError:
        raise
    except requests.RequestException as exc:
        # Some Windows CDN TLS connections fail in Python/OpenSSL while the
        # system Schannel client succeeds. Keep certificate checks enabled in
        # both clients and fall back only to the installed Windows curl.
        LOGGER.warning("Python garment download failed (%s); trying Windows HTTPS client", type(exc).__name__)
        raw = _fetch_garment_windows(garment_url)
    return _decode_data_image(base64.b64encode(raw).decode("ascii"), field="garment_image")


def _fetch_garment_windows(url: str) -> bytes:
    curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    if os.name != "nt" or not curl.is_file():
        raise LocalTryOnRequestError("garment image could not be downloaded")
    try:
        # Store the response in a temporary file instead of buffering arbitrary
        # network data in memory. curl also enforces size/time/HTTPS limits.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                [str(curl), "--fail", "--location", "--max-time", "30",
                 "--max-filesize", str(MAX_IMAGE_BYTES), "--proto", "=https",
                 "--proto-redir", "=https", "--silent", "--show-error", url],
                stdout=output, stderr=subprocess.PIPE, timeout=35, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                raise LocalTryOnRequestError("garment image could not be downloaded")
            size = output.tell()
            if not size or size > MAX_IMAGE_BYTES:
                raise LocalTryOnRequestError("garment image is empty or too large")
            output.seek(0)
            return output.read(MAX_IMAGE_BYTES)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LocalTryOnRequestError("garment image could not be downloaded") from exc


class FashnVtonRuntime:
    """Lazy, single-instance pipeline wrapper.

    The model is expensive to initialize and inference is GPU-memory bound, so
    requests are serialized.  This also prevents two browser uploads from
    competing for the 8GB card.
    """

    def __init__(self, weights_dir: Path, device: str | None = None) -> None:
        self.weights_dir = Path(weights_dir)
        self.device = device or None
        self._pipeline: Any = None
        self._load_error: Exception | None = None
        self._lock = RLock()
        self._slots = BoundedSemaphore(4)

    def _get_pipeline(self) -> Any:
        with self._lock:
            if self._pipeline is not None:
                return self._pipeline
            if self._load_error is not None:
                raise RuntimeError("FASHN pipeline failed to initialize") from self._load_error
            try:
                from fashn_vton import TryOnPipeline

                self._pipeline = TryOnPipeline(weights_dir=str(self.weights_dir), device=self.device)
            except Exception as exc:  # keep health endpoint available with a useful 503
                self._load_error = exc
                raise RuntimeError("FASHN pipeline failed to initialize") from exc
            return self._pipeline

    def generate(
        self,
        *,
        person_image: Image.Image,
        garment_image: Image.Image,
        category: str,
        garment_photo_type: str,
        num_timesteps: int,
    ) -> str:
        if not self._slots.acquire(blocking=False):
            raise ModelBusyError("The shared model service is busy; retry later")
        try:
            with self._lock:
                pipeline = self._get_pipeline()
                result = pipeline(
                    person_image=person_image,
                    garment_image=garment_image,
                    category=category,
                    garment_photo_type=garment_photo_type,
                    num_timesteps=num_timesteps,
                    num_samples=1,
                )
                if not getattr(result, "images", None) or not isinstance(result.images[0], Image.Image):
                    raise RuntimeError("FASHN pipeline returned no image")
                output = result.images[0]
                if output.width * output.height > MAX_IMAGE_PIXELS or max(output.size) > MAX_IMAGE_SIDE:
                    raise RuntimeError("FASHN pipeline returned an oversized image")
                with io.BytesIO() as stream:
                    output.convert("RGB").save(stream, format="JPEG", quality=94, optimize=True)
                    data = stream.getvalue()
                if not data or len(data) > MAX_IMAGE_BYTES:
                    raise RuntimeError("FASHN pipeline returned an invalid image")
                return base64.b64encode(data).decode("ascii")
        finally:
            self._slots.release()


class FashnVtonHandler(BaseHTTPRequestHandler):
    server_version = "FayFashnVton/2.0"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    @property
    def runtime(self) -> FashnVtonRuntime:
        return self.server.runtime  # type: ignore[attr-defined]

    @property
    def auth_token(self) -> str:
        return self.server.auth_token  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            LOGGER.info("Client disconnected before the model response was delivered")

    def _authorized(self) -> bool:
        if not self.auth_token:
            return True
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.auth_token}"
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        path = self.path.rstrip("/")
        if path in {"/health", "/health/auth"}:
            if path == "/health/auth" and not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                return
            loaded = self.runtime._pipeline is not None
            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "service": "fashn-vton-1.5",
                "protocol_version": 2,
                "accepted_inputs": ["garment_image", "garment_image_url"],
                "serialized_inference": True,
                "pipeline_loaded": loaded,
                "initialization_failed": self.runtime._load_error is not None,
                "auth_required": bool(self.auth_token),
            })
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path.rstrip("/") != "/tryon":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_request", "message": "A single Content-Length is required"})
            return
        length = int(lengths[0])
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "request_too_large"})
            return
        person = garment = None
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise LocalTryOnRequestError("Request body is incomplete")
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise LocalTryOnRequestError("JSON body must contain valid UTF-8 JSON") from exc
            if not isinstance(payload, dict):
                raise LocalTryOnRequestError("JSON body must be an object")
            category = str(payload.get("category") or "tops").strip().lower()
            photo_type = str(payload.get("garment_photo_type") or "flat-lay").strip().lower()
            if category not in VALID_CATEGORIES:
                raise LocalTryOnRequestError("category must be tops, bottoms or one-pieces")
            if photo_type not in VALID_PHOTO_TYPES:
                raise LocalTryOnRequestError("garment_photo_type must be model or flat-lay")
            try:
                if isinstance(payload.get("num_timesteps"), bool):
                    raise ValueError("Boolean is not a timestep count")
                timesteps = int(payload.get("num_timesteps", 20))
            except (TypeError, ValueError) as exc:
                raise LocalTryOnRequestError("num_timesteps must be an integer") from exc
            if not 1 <= timesteps <= 50:
                raise LocalTryOnRequestError("num_timesteps must be between 1 and 50")
            if ("garment_image" in payload) == ("garment_image_url" in payload):
                raise LocalTryOnRequestError("Provide exactly one of garment_image or garment_image_url")
            person = _decode_data_image(payload.get("person_image"), field="person_image")
            garment = (
                _decode_data_image(payload["garment_image"], field="garment_image")
                if "garment_image" in payload else _fetch_garment(payload["garment_image_url"])
            )
            image = self.runtime.generate(
                person_image=person,
                garment_image=garment,
                category=category,
                garment_photo_type=photo_type,
                num_timesteps=timesteps,
            )
            self._send_json(HTTPStatus.OK, {"ok": True, "image_base64": image, "format": "jpeg"})
        except LocalTryOnRequestError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_request", "message": str(exc)})
        except ModelBusyError:
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": "model_busy", "message": "共享试衣服务繁忙，请稍后重试"})
        except TimeoutError:
            self._send_json(HTTPStatus.REQUEST_TIMEOUT, {"ok": False, "error": "request_timeout"})
        except Exception:  # do not expose model internals to the browser
            LOGGER.exception("FASHN local inference failed")
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"ok": False, "error": "model_unavailable", "message": "本地 FASHN 模型暂不可用，请查看服务日志"},
            )
        finally:
            if person is not None:
                person.close()
            if garment is not None:
                garment.close()

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)


class FashnVtonServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], runtime: FashnVtonRuntime, auth_token: str = "") -> None:
        host = _loopback_host(address[0])
        if ":" in host:
            self.address_family = socket.AF_INET6
        self.runtime = runtime
        self.auth_token = auth_token
        super().__init__((host, address[1]), FashnVtonHandler)


def _loopback_host(host: str) -> str:
    if host == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("The model service must bind a literal loopback address") from exc
    if not address.is_loopback:
        raise ValueError("The model service may only bind a loopback address")
    return str(address)


def run_server(
    *,
    weights_dir: Path,
    host: str = "127.0.0.1",
    port: int = 7860,
    device: str | None = None,
    auth_token: str = "",
) -> None:
    host = _loopback_host(host)
    runtime = FashnVtonRuntime(weights_dir=weights_dir, device=device)
    server = FashnVtonServer((host, port), runtime, auth_token=auth_token)
    LOGGER.info("FASHN VTON local service listening at http://%s:%s", host, port)
    LOGGER.info("Weights directory: %s", weights_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping local FASHN service")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="FASHN VTON v1.5 local HTTP service for Fay")
    parser.add_argument(
        "--weights-dir",
        default=os.environ.get("FAY_FASHN_VTON_WEIGHTS_DIR") or "weights",
        help="FASHN VTON weights directory (default: ./weights)",
    )
    parser.add_argument("--host", default=os.environ.get("FAY_FASHN_VTON_HOST") or "127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FAY_FASHN_VTON_PORT", "7860")))
    parser.add_argument("--device", default=os.environ.get("FAY_FASHN_VTON_DEVICE") or None)
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("FAY_TRYON_LOCAL_TOKEN") or "",
        help="Optional bearer token; keep it in an environment variable",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_server(
        weights_dir=Path(args.weights_dir),
        host=args.host,
        port=args.port,
        device=args.device,
        auth_token=args.auth_token,
    )


if __name__ == "__main__":
    main()
