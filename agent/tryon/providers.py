from __future__ import annotations

from abc import ABC, abstractmethod
import base64
from functools import lru_cache
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from agent.catalog_images import catalog_image_path


def _catalog_image_data_uri(image_url: str) -> str | None:
    try:
        path = catalog_image_path(image_url)
        if path is None:
            return None
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[path.suffix.lower()]
        return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    except (OSError, ValueError) as exc:
        raise TryOnProviderError("garment_image_unavailable", "无法读取已登记的本地服装图片") from exc


@lru_cache(maxsize=1)
def _verified_tryon_references() -> dict[str, dict[str, Any]]:
    """仅使用单独登记过的试衣参考图，不把商城封面当成平铺图。"""
    catalog_dir = os.environ.get("FAY_PRODUCT_CATALOG_DIR", "").strip()
    root = Path(catalog_dir).expanduser() if catalog_dir else Path(__file__).resolve().parents[1] / "data"
    path = root / "tryon_references.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    records = payload.get("references", []) if isinstance(payload, dict) else []
    return {
        str(record["product_id"]): record
        for record in records
        if isinstance(record, dict) and record.get("product_id") and record.get("tryon_image_url")
    }


def _category_for_garment(garment: dict[str, Any]) -> str:
    explicit = str(garment.get("tryon_category") or "").strip().lower()
    if explicit in {"tops", "bottoms", "one-pieces"}:
        return explicit
    text = f"{garment.get('category', '')} {garment.get('name', '')}".lower()
    # 连衣裙、礼服和连体衣覆盖全身；半身裙/短裙仍属于 bottoms。
    if any(token in text for token in ("连衣裙", "吊带裙", "礼服", "连体", "dress", "jumpsuit", "one-piece")):
        return "one-pieces"
    if any(token in text for token in ("裤", "半身裙", "短裙", "长裙", "裙子", "bottom", "pants", "skirt")):
        return "bottoms"
    return "tops"


def resolve_tryon_reference(garment: dict[str, Any]) -> dict[str, Any]:
    """统一解析实际试衣图及已知元数据，不从商品 SKU 推断参考图颜色。"""
    reference = dict(garment)
    if not str(garment.get("tryon_image_url") or "").strip():
        verified = _verified_tryon_references().get(str(garment.get("product_id") or ""))
        if verified:
            reference.update(verified)
    photo_type = str(reference.get("tryon_garment_photo_type") or "model").strip().lower()
    return {
        "image_url": str(reference.get("tryon_image_url") or garment.get("image_url") or "").strip(),
        "color_label": str(reference.get("color_label") or "").strip() or None,
        "category": _category_for_garment(reference),
        "photo_type": photo_type if photo_type in {"flat-lay", "model"} else "model",
    }


class TryOnProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TryOnProvider(ABC):
    """虚拟试衣生成协议。

    第三方实现应在此边界内读取密钥，不要把密钥写入仓库或返回给前端。
    """

    name = "abstract"
    development_placeholder = False

    @abstractmethod
    def generate(
        self,
        *,
        person_image: Path,
        garment: dict[str, Any],
        beautify: str,
        output_path: Path,
    ) -> None:
        raise NotImplementedError


class DisabledTryOnProvider(TryOnProvider):
    name = "disabled"

    def generate(self, **_: Any) -> None:
        raise TryOnProviderError(
            "tryon_provider_not_configured",
            "AI 虚拟试衣服务尚未配置图像模型，请先配置合规的试衣服务 Provider。",
        )


class DevelopmentPlaceholderProvider(TryOnProvider):
    """仅供本机联调的占位实现，绝不宣称生成了真实试穿效果。"""

    name = "development_placeholder"
    development_placeholder = True

    def generate(
        self,
        *,
        person_image: Path,
        garment: dict[str, Any],
        beautify: str,
        output_path: Path,
    ) -> None:
        from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

        with Image.open(person_image) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
        if beautify == "light":
            image = ImageEnhance.Brightness(image).enhance(1.04)
            image = ImageEnhance.Color(image).enhance(1.03)
            image = image.filter(ImageFilter.SMOOTH)
        elif beautify == "natural":
            image = ImageEnhance.Contrast(image).enhance(1.02)

        # 这是明显带水印的联调图，不叠加服装、不冒充模型输出。
        draw = ImageDraw.Draw(image, "RGBA")
        banner_height = max(96, min(180, image.height // 7))
        draw.rectangle((0, image.height - banner_height, image.width, image.height), fill=(9, 42, 42, 225))
        product_name = str(garment.get("name") or "Selected garment")[:42]
        draw.text((24, image.height - banner_height + 18), "DEV PLACEHOLDER - NOT AI TRY-ON", fill=(255, 215, 128, 255))
        draw.text((24, image.height - banner_height + 52), product_name, fill=(255, 255, 255, 255))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path, format="JPEG", quality=90, optimize=True)


class FashnTryOnProvider(TryOnProvider):
    """FASHN `tryon-v1.6` 适配器。

    密钥仅从环境变量读取。请求 base64 输出，避免生成图在第三方 URL 上长时间保留。
    """

    name = "fashn"

    def __init__(
        self,
        api_key: str,
        *,
        api_base: str = "https://api.fashn.ai/v1",
        http: Any = requests,
        timeout_seconds: int = 120,
        poll_interval: float = 1.5,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.api_base = api_base.rstrip("/")
        self.http = http
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval

    @staticmethod
    def _response_json(response: Any) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception as exc:
            raise TryOnProviderError("tryon_provider_invalid_response", "试衣服务返回了无法解析的响应") from exc
        if not isinstance(payload, dict):
            raise TryOnProviderError("tryon_provider_invalid_response", "试衣服务返回格式不正确")
        return payload

    @staticmethod
    def _write_base64_result(value: str, output_path: Path) -> None:
        encoded = str(value or "")
        if encoded.startswith("data:"):
            encoded = encoded.partition(",")[2]
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise TryOnProviderError("tryon_provider_invalid_image", "试衣服务返回的图片无效") from exc
        if not data or len(data) > 16 * 1024 * 1024:
            raise TryOnProviderError("tryon_provider_invalid_image", "试衣服务返回的图片大小异常")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)

    def generate(
        self,
        *,
        person_image: Path,
        garment: dict[str, Any],
        beautify: str,
        output_path: Path,
    ) -> None:
        if not self.api_key:
            raise TryOnProviderError("tryon_provider_not_configured", "FASHN_API_KEY 尚未配置")
        reference = resolve_tryon_reference(garment)
        garment_image = reference["image_url"]
        local_image = _catalog_image_data_uri(garment_image)
        if not local_image and not garment_image.startswith("https://"):
            raise TryOnProviderError("garment_image_unavailable", "商品图必须来自本地登记目录或可信 HTTPS 地址")
        model_data = base64.b64encode(person_image.read_bytes()).decode("ascii")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        # FASHN 当前接口没有安全、可验证的“瘦身”参数。beautify 只作为本地服务记录，
        # 不向模型发送改变体型、肤色或商品颜色的指令。
        request_body = {
            "model_name": "tryon-v1.6",
            "inputs": {
                "model_image": f"data:image/jpeg;base64,{model_data}",
                "garment_image": local_image or garment_image,
                "return_base64": True,
            },
        }
        try:
            response = self.http.post(
                f"{self.api_base}/run",
                json=request_body,
                headers=headers,
                timeout=30,
            )
        except Exception as exc:
            raise TryOnProviderError("tryon_provider_unavailable", "试衣服务当前不可用，请稍后重试") from exc
        if int(getattr(response, "status_code", 500)) >= 400:
            raise TryOnProviderError("tryon_provider_rejected", "试衣服务拒绝了本次请求，请检查服务配置或照片")
        run_payload = self._response_json(response)
        job_id = str(run_payload.get("id") or "").strip()
        if not job_id:
            raise TryOnProviderError("tryon_provider_invalid_response", "试衣服务没有返回任务编号")

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            try:
                status_response = self.http.get(
                    f"{self.api_base}/status/{job_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=20,
                )
            except Exception as exc:
                raise TryOnProviderError("tryon_provider_unavailable", "读取试衣结果失败，请稍后重试") from exc
            if int(getattr(status_response, "status_code", 500)) >= 400:
                raise TryOnProviderError("tryon_provider_rejected", "试衣服务任务查询失败")
            payload = self._response_json(status_response)
            status = str(payload.get("status") or "").lower()
            if status in {"completed", "succeeded"}:
                output = payload.get("output")
                value = output[0] if isinstance(output, list) and output else output
                if not isinstance(value, str):
                    raise TryOnProviderError("tryon_provider_invalid_response", "试衣服务没有返回结果图片")
                self._write_base64_result(value, output_path)
                return
            if status in {"failed", "error", "canceled", "cancelled"}:
                raise TryOnProviderError("tryon_generation_failed", "本次试衣生成失败，请换一张清晰全身照重试")
            time.sleep(self.poll_interval)
        raise TryOnProviderError("tryon_generation_timeout", "试衣生成超时，请稍后重试")


class FashnLocalTryOnProvider(TryOnProvider):
    """FASHN VTON v1.5 本地 HTTP 适配器。

    FASHN VTON 的 GPU 依赖安装在独立环境中，本进程只负责把已校验的人像和
    商品图片引用交给本机服务，并将服务返回的 base64 图片写入 Fay 的结果路径。
    本地服务协议见 :mod:`agent.tryon.local_service`。
    """

    name = "fashn_local"

    def __init__(
        self,
        *,
        endpoint: str = "http://127.0.0.1:7860/tryon",
        http: Any = requests,
        timeout_seconds: int = 600,
        token: str = "",
        num_timesteps: int = 20,
    ) -> None:
        self.endpoint = str(endpoint or "").strip().rstrip("/")
        self.http = http
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.token = str(token or "").strip()
        self.num_timesteps = max(1, min(50, int(num_timesteps)))

    @staticmethod
    def _category_for_garment(garment: dict[str, Any]) -> str:
        return _category_for_garment(garment)

    @staticmethod
    def _result_value(payload: dict[str, Any]) -> str | None:
        value: Any = payload.get("image_base64") or payload.get("image") or payload.get("output")
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, dict):
            value = value.get("image_base64") or value.get("image") or value.get("url")
        return value if isinstance(value, str) else None

    def generate(
        self,
        *,
        person_image: Path,
        garment: dict[str, Any],
        beautify: str,
        output_path: Path,
    ) -> None:
        del beautify  # 美化仍由 TryOnService 在结果落盘前按安全规则处理。
        if not self.endpoint:
            raise TryOnProviderError("tryon_provider_not_configured", "FAY_TRYON_LOCAL_URL 尚未配置")
        reference = resolve_tryon_reference(garment)
        garment_image = reference["image_url"]
        local_image = _catalog_image_data_uri(garment_image)
        if not local_image and not garment_image.startswith("https://"):
            raise TryOnProviderError("garment_image_unavailable", "商品图必须来自本地公开目录或可信 HTTPS 地址")
        try:
            person_data = base64.b64encode(person_image.read_bytes()).decode("ascii")
        except OSError as exc:
            raise TryOnProviderError("tryon_provider_invalid_image", "无法读取待试穿的人像图片") from exc

        request_body = {
            "person_image": f"data:image/jpeg;base64,{person_data}",
            "category": reference["category"],
            "garment_photo_type": reference["photo_type"],
            "num_timesteps": self.num_timesteps,
        }
        request_body["garment_image" if local_image else "garment_image_url"] = local_image or garment_image
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.http.post(
                self.endpoint,
                json=request_body,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise TryOnProviderError("tryon_provider_unavailable", "本地 FASHN 试衣服务当前不可用，请先启动服务") from exc
        if int(getattr(response, "status_code", 500)) == 429:
            raise TryOnProviderError("tryon_provider_busy", "共享试衣服务繁忙，请稍后重试")
        if int(getattr(response, "status_code", 500)) >= 400:
            raise TryOnProviderError("tryon_provider_rejected", "本地 FASHN 试衣服务拒绝了本次请求，请检查服务日志")
        payload = FashnTryOnProvider._response_json(response)
        value = self._result_value(payload)
        if not value:
            raise TryOnProviderError("tryon_provider_invalid_response", "本地 FASHN 服务没有返回结果图片")
        FashnTryOnProvider._write_base64_result(value, output_path)


def _local_token_from_environment(endpoint: str) -> str:
    if "FAY_TRYON_LOCAL_TOKEN" in os.environ:
        return os.environ["FAY_TRYON_LOCAL_TOKEN"].strip()
    # Never forward the local shared secret to an unrelated configured service.
    try:
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.port != 7860 or parsed.username is not None or parsed.password is not None
                or parsed.path.rstrip("/") != "/tryon" or parsed.query or parsed.fragment):
            return ""
    except ValueError:
        return ""
    try:
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        token_path = local_app_data / "FayWardrobe" / "FashnShared" / "service.token"
        return token_path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError, RuntimeError):
        return ""


def provider_from_environment() -> TryOnProvider:
    provider_name = (os.environ.get("FAY_TRYON_PROVIDER") or "disabled").strip().lower()
    if provider_name in {"dev", "development", "development_placeholder"}:
        return DevelopmentPlaceholderProvider()
    if provider_name == "fashn":
        return FashnTryOnProvider(os.environ.get("FASHN_API_KEY") or "")
    if provider_name in {"fashn_local", "local_fashn", "fashn-vton", "fashn_vton_local"}:
        def _int_env(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        endpoint = os.environ.get("FAY_TRYON_LOCAL_URL") or "http://127.0.0.1:7860/tryon"
        return FashnLocalTryOnProvider(
            endpoint=endpoint,
            token=_local_token_from_environment(endpoint),
            timeout_seconds=_int_env("FAY_TRYON_LOCAL_TIMEOUT_SECONDS", 600),
            num_timesteps=_int_env("FAY_TRYON_LOCAL_STEPS", 20),
        )
    # 未知值同样按未配置处理，避免静默降级成假结果。
    return DisabledTryOnProvider()
