from __future__ import annotations

import io
import hashlib
import os
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, BinaryIO

from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError

from agent.store.service import CommerceService, get_commerce_service

from .providers import TryOnProvider, TryOnProviderError, provider_from_environment, resolve_tryon_reference


MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_NORMALIZED_IMAGE_SIDE = 2048
MIN_WIDTH = 320
MIN_HEIGHT = 480
RESULT_TTL_SECONDS = 30 * 60
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
ALLOWED_BEAUTIFY = {"natural", "light"}
APPAREL_PATTERN = ("服饰", "服装", "上衣", "外套", "衬衫", "裙", "裤", "卫衣", "毛衣", "针织")
DEFAULT_RUNTIME_DIR = Path(__file__).resolve().parents[1] / "data" / "runtime" / "tryon"


class TryOnError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class TryOnJob:
    job_id: str
    session_id: str
    product_id: str
    result_path: Path
    created_at: float
    expires_at: float
    development_placeholder: bool


class TryOnService:
    def __init__(
        self,
        *,
        provider: TryOnProvider | None = None,
        commerce_service: CommerceService | None = None,
        runtime_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
        cleanup_interval: float = 60.0,
    ) -> None:
        self.provider = provider or provider_from_environment()
        self.commerce_service = commerce_service or get_commerce_service()
        self.runtime_dir = Path(runtime_dir or os.environ.get("FAY_TRYON_RUNTIME_DIR") or DEFAULT_RUNTIME_DIR)
        self.clock = clock
        self._jobs: dict[str, TryOnJob] = {}
        self._lock = threading.RLock()
        self._active_jobs: set[Path] = set()
        self._stop = threading.Event()
        self._cleanup_expired()
        self._reaper = threading.Thread(target=self._reap, args=(max(0.01, cleanup_interval),), daemon=True, name="tryon-result-cleanup")
        self._reaper.start()

    def _reap(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self._cleanup_expired()
            except OSError:
                # 文件暂时被下载请求占用时，下个周期重试。
                pass

    def close(self) -> None:
        self._stop.set()
        self._reaper.join(timeout=2)

    def status(self, product_id: str | None = None) -> dict[str, Any]:
        configured = self.provider.name != "disabled"
        result = {
            "configured": configured,
            "provider": self.provider.name if configured else None,
            "development_placeholder": bool(self.provider.development_placeholder),
            "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            "accepted_types": ["image/jpeg", "image/png", "image/webp"],
            "result_ttl_minutes": RESULT_TTL_SECONDS // 60,
            "privacy_notice": (
                "照片仅用于本次试衣请求；原图在生成请求结束前删除，结果30分钟后失效，后台在下一清理周期（通常60秒内）删除，也可立即删除。"
                "配置第三方 Provider 后，照片可能发送给该服务，正式上线前必须展示其隐私政策并取得单独同意。"
            ),
        }
        if product_id:
            result["reference"] = resolve_tryon_reference(self._verified_product(product_id))
        return result

    @staticmethod
    def _is_apparel(product: dict[str, Any]) -> bool:
        text = f"{product.get('category', '')} {product.get('name', '')}"
        return any(token in text for token in APPAREL_PATTERN)

    def _verified_product(self, product_id: str) -> dict[str, Any]:
        normalized = str(product_id or "").strip()
        if not normalized or len(normalized) > 96:
            raise TryOnError("invalid_product", "请选择有效的服装商品")
        try:
            product = self.commerce_service.get_product(normalized)
        except Exception as exc:
            if getattr(exc, "code", "") == "product_not_found":
                raise TryOnError("product_not_found", "所选商品不在已核验商品目录中", 404) from exc
            raise
        if not self._is_apparel(product):
            raise TryOnError("product_not_apparel", "该商品不是可用于虚拟试衣的服装商品")
        if not product.get("image_url"):
            raise TryOnError("garment_image_unavailable", "该商品缺少已核验的服装图片，暂时不能试衣")
        return product

    @staticmethod
    def _read_upload(stream: BinaryIO) -> bytes:
        data = stream.read(MAX_UPLOAD_BYTES + 1)
        if not data:
            raise TryOnError("empty_image", "请选择一张全身照片")
        if len(data) > MAX_UPLOAD_BYTES:
            raise TryOnError("image_too_large", "照片不能超过 64MB", 413)
        return data

    @staticmethod
    def _normalize_image(data: bytes, target: Path) -> None:
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                if (image.format or "").upper() not in ALLOWED_FORMATS:
                    raise TryOnError("unsupported_image", "仅支持 JPEG、PNG 或 WebP 图片")
                width, height = image.size
                if width < MIN_WIDTH or height < MIN_HEIGHT:
                    raise TryOnError("image_too_small", "建议上传至少 320×480 的清晰全身照")
                if width * height > MAX_IMAGE_PIXELS:
                    raise TryOnError("image_too_large", "图片像素过大，请压缩后重试", 413)
                normalized = ImageOps.exif_transpose(image).convert("RGB")
                # 原图可达 64MB；保持比例缩小后再送入模型，兼容本地服务的
                # 16MB 图片上限，也避免高分辨率原图增加模型输入开销。
                normalized.thumbnail(
                    (MAX_NORMALIZED_IMAGE_SIDE, MAX_NORMALIZED_IMAGE_SIDE),
                    Image.Resampling.LANCZOS,
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                # 重编码会剥离 EXIF、定位和相机信息。
                normalized.save(target, format="JPEG", quality=94, optimize=True)
        except TryOnError:
            raise
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise TryOnError("invalid_image", "文件不是有效、安全的图片") from exc

    def _cleanup_expired(self) -> None:
        now = self.clock()
        with self._lock:
            expired = [job_id for job_id, job in self._jobs.items() if job.expires_at <= now]
            for job_id in expired:
                job = self._jobs[job_id]
                shutil.rmtree(job.result_path.parent, ignore_errors=True)
                if not job.result_path.parent.exists():
                    self._jobs.pop(job_id, None)
        # 进程重启后内存任务表会丢失，因此同时按目录时间清理遗留结果。
        if self.runtime_dir.is_dir():
            for session_dir in self.runtime_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                for job_dir in session_dir.iterdir():
                    if not job_dir.is_dir():
                        continue
                    try:
                        stale = now - job_dir.stat().st_mtime >= RESULT_TTL_SECONDS
                    except OSError:
                        stale = False
                    with self._lock:
                        protected = job_dir in self._active_jobs or any(job.result_path.parent == job_dir for job in self._jobs.values())
                        if stale and not protected:
                            shutil.rmtree(job_dir, ignore_errors=True)
                try:
                    session_dir.rmdir()
                except OSError:
                    pass

    @staticmethod
    def _finalize_result(path: Path, beautify: str) -> None:
        """验证并重编码 Provider 结果；轻度模式只调整光线，不做几何或生成式重绘。"""
        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise TryOnError("tryon_result_too_large", "试衣结果像素异常", 502)
            if beautify == "light":
                image = ImageEnhance.Brightness(image).enhance(1.015)
                image = ImageEnhance.Contrast(image).enhance(1.01)
            image.save(path, format="JPEG", quality=92, optimize=True)
        except TryOnError:
            raise
        except (UnidentifiedImageError, OSError) as exc:
            raise TryOnError("invalid_provider_result", "试衣服务返回的结果不是有效图片", 502) from exc

    def create(
        self,
        *,
        session_id: str,
        product_id: str,
        beautify: str,
        consent: bool,
        upload_stream: BinaryIO,
    ) -> dict[str, Any]:
        self._cleanup_expired()
        if consent is not True:
            raise TryOnError("privacy_consent_required", "请先确认照片处理与临时存储说明")
        beautify = str(beautify or "natural").strip().lower()
        if beautify not in ALLOWED_BEAUTIFY:
            raise TryOnError("invalid_beautify", "美化方式仅支持自然或轻度美化")
        product = self._verified_product(product_id)
        reference = resolve_tryon_reference(product)
        # 固定本次展示给用户的参考，Provider 不再重新选择另一张图或颜色。
        garment = {
            **product,
            "image_url": reference["image_url"],
            "tryon_image_url": reference["image_url"],
            "tryon_category": reference["category"],
            "tryon_garment_photo_type": reference["photo_type"],
            "color_label": reference["color_label"],
        }
        data = self._read_upload(upload_stream)
        job_id = secrets.token_urlsafe(18)
        safe_session_dir = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()
        job_dir = self.runtime_dir / safe_session_dir / job_id
        source_path = job_dir / "source.jpg"
        result_path = job_dir / "result.jpg"
        with self._lock:
            self._active_jobs.add(job_dir)
        try:
            self._normalize_image(data, source_path)
            self.provider.generate(
                person_image=source_path,
                garment=garment,
                beautify=beautify,
                output_path=result_path,
            )
            if not result_path.is_file() or result_path.stat().st_size <= 0:
                raise TryOnError("empty_provider_result", "试衣服务没有返回有效图片", 502)
            self._finalize_result(result_path, beautify)
            source_path.unlink(missing_ok=True)
            created_at = self.clock()
            job = TryOnJob(
                job_id=job_id,
                session_id=session_id,
                product_id=str(product["product_id"]),
                result_path=result_path,
                created_at=created_at,
                expires_at=created_at + RESULT_TTL_SECONDS,
                development_placeholder=bool(self.provider.development_placeholder),
            )
            with self._lock:
                self._jobs[job_id] = job
        except TryOnProviderError as exc:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise TryOnError(exc.code, str(exc), 503) from exc
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        finally:
            with self._lock:
                self._active_jobs.discard(job_dir)
        return {
            "job_id": job_id,
            "product_id": job.product_id,
            "product_name": product.get("name"),
            "reference": reference,
            "beautify": beautify,
            "expires_in": RESULT_TTL_SECONDS,
            "development_placeholder": job.development_placeholder,
            "notice": (
                "开发模式占位图，不是 AI 试穿结果。" if job.development_placeholder
                else (
                    "AI 生成效果仅供穿搭参考；已做轻微光线优化，不改变体型或商品颜色。"
                    if beautify == "light"
                    else "AI 生成效果仅供穿搭参考，不代表真实尺码、面料垂感或最终上身效果。"
                )
            ),
        }

    def get_job(self, session_id: str, job_id: str) -> TryOnJob:
        self._cleanup_expired()
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
        if not job or job.expires_at <= self.clock() or job.session_id != session_id or not job.result_path.is_file():
            raise TryOnError("tryon_result_not_found", "试衣结果不存在、已过期或不属于当前会话", 404)
        return job

    def delete(self, session_id: str, job_id: str) -> None:
        job = self.get_job(session_id, job_id)
        with self._lock:
            shutil.rmtree(job.result_path.parent, ignore_errors=True)
            if job.result_path.parent.exists():
                # 下载占用文件时立即失效，并由后台重试删除。
                job.expires_at = self.clock()
            else:
                self._jobs.pop(job.job_id, None)


_SERVICE: TryOnService | None = None
_SERVICE_LOCK = threading.Lock()


def get_tryon_service() -> TryOnService:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = TryOnService()
    return _SERVICE
