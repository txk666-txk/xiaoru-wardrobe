from __future__ import annotations

import io
import copy
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .engine import StylingEngine, parse_feedback
from .preferences import PreferenceStore, clean_preferences
from .vision import LocalVision, StylingError

MAX_UPLOAD = 64 * 1024 * 1024
TTL = 30 * 60
ROOT = Path(__file__).resolve().parents[1] / "data" / "runtime" / "styling"


def normalize_photo(stream):
    data = stream.read(MAX_UPLOAD + 1)
    if not data or len(data) > MAX_UPLOAD:
        raise StylingError("invalid_photo_size", "请选择非空且不超过64MB的图片", 413)
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                raise StylingError("invalid_photo_type", "仅支持静态 JPEG、PNG、WebP 图片")
            if image.width * image.height > 24_000_000 or min(image.size) < 128:
                raise StylingError("invalid_photo_dimensions", "请使用至少128像素且不超过2400万像素的清晰照片")
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            normalized.thumbnail((768, 768), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            normalized.save(output, "JPEG", quality=90)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise StylingError("invalid_photo", "文件不是有效图片，请更换照片") from exc


class StylingService:
    def __init__(self, *, vision=None, engine=None, root=None, clock=time.time):
        self.root = Path(root or os.environ.get("FAY_STYLE_RUNTIME_DIR") or ROOT)
        self.vision = vision or LocalVision()
        self.engine = engine or StylingEngine()
        self.preferences = PreferenceStore(self.root / "preferences.db")
        self.clock = clock
        self._jobs = {}
        self._conversations = {}
        self._voice_bindings = {}
        self._turn_locks = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fay-style")
        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap, daemon=True, name="fay-style-cleanup")
        self._reaper.start()

    def _reap(self):
        while not self._stop.wait(60):
            with self._lock:
                self._cleanup()

    def close(self):
        self._stop.set()
        self._executor.shutdown(wait=True)

    def _cleanup(self):
        # Photos are cleared after analysis; completed suggestions expire in RAM.
        expired = [key for key, job in self._jobs.items()
                   if job["expires_at"] <= self.clock() and job["status"] not in {"queued", "running"}]
        for key in expired:
            del self._jobs[key]
        for key in [key for key, state in self._conversations.items() if state['expires_at'] <= self.clock()]:
            del self._conversations[key]
            self._turn_locks.pop(key, None)
        for key in [key for key, binding in self._voice_bindings.items() if binding['expires_at'] <= self.clock()]:
            del self._voice_bindings[key]

    def conversation(self, owner):
        with self._lock:
            self._cleanup()
            return copy.deepcopy(self._conversations.get(owner))

    def turn_lock(self, owner):
        with self._lock:
            self.update_conversation(owner)
            return self._turn_locks.setdefault(owner, threading.RLock())

    def update_conversation(self, owner, **values):
        with self._lock:
            self._cleanup()
            if owner not in self._conversations and len(self._conversations) >= 128:
                raise StylingError('style_busy', '会话数量已达上限，请稍后再试', 429)
            state = self._conversations.setdefault(owner, {'options': {}, 'waiting': [], 'active': True})
            state.update(copy.deepcopy(values), expires_at=self.clock() + TTL)
            return copy.deepcopy(state)

    def publish(self, owner, response):
        presentation = {**copy.deepcopy(response.presentation), 'revision': secrets.token_hex(16),
                        'text': response.text, 'tool_calls': copy.deepcopy(response.tool_calls)}
        self.update_conversation(owner, presentation=presentation)
        response.presentation = presentation

    def bind_voice(self, owner, username, enabled=True):
        with self._lock:
            self._cleanup()
            current = self._voice_bindings.get(username)
            if enabled and any(binding['owner'] != owner for binding in self._voice_bindings.values()):
                raise StylingError('voice_in_use', '该客户的本机麦克风已关联另一个窗口，请先在那个窗口关闭麦克风', 409)
            if enabled:
                self._voice_bindings.clear()
                self._voice_bindings[username] = {'owner': owner, 'expires_at': self.clock() + 90}
            elif current and current['owner'] == owner:
                del self._voice_bindings[username]

    def voice_owner(self, username):
        with self._lock:
            self._cleanup()
            binding = self._voice_bindings.get(username)
            return binding['owner'] if binding else None

    def local_voice_context(self):
        with self._lock:
            self._cleanup()
            return next(((username, binding['owner']) for username, binding in self._voice_bindings.items()), (None, None))

    @staticmethod
    def view(job):
        return copy.deepcopy({key: value for key, value in job.items() if key not in {"owner", "image", "options"}})

    def create(self, owner, *, mode, options=None, stream=None, consent=False, defer=False):
        if mode not in {"person", "garment", "text"}:
            raise StylingError("invalid_mode", "请选择本人照片、已有衣服或文字推荐")
        if mode != "text" and consent is not True:
            raise StylingError("photo_consent_required", "看图前请同意本机处理照片")
        options = {**self.preferences.get(owner), **clean_preferences({} if options is None else options)}
        # Validate before sending photos to the model; fail explicitly, never invent observations.
        if mode != "text":
            status = self.vision.status()
            if not status["available"]:
                raise StylingError("vision_not_ready", status["message"], 503)
        image = normalize_photo(stream) if mode != "text" and stream is not None else None
        if mode != "text" and image is None:
            raise StylingError("photo_required", "请上传一张照片")
        with self._lock:
            self._cleanup()
            active = [j for j in self._jobs.values() if j["status"] in {"uploaded", "queued", "running"}]
            if len(active) >= 3 or any(j["owner"] == owner for j in active) or len(self._jobs) >= 32:
                raise StylingError("style_busy", "已有搭配任务处理中，请等待完成或清除旧结果后再试", 429)
            key = secrets.token_hex(16)
            job = {"job_id": key, "owner": owner, "mode": mode, "status": "uploaded" if defer else "queued", "image": image,
                   "options": options, "result": None, "message": "已排队，等待本地穿搭分析",
                   "expires_at": self.clock() + TTL, "revision": secrets.token_hex(8), "error": None}
            self._jobs[key] = job
            if not defer:
                self._executor.submit(self._run, key)
            return self.view(job)

    def inspect_photo(self, owner, key):
        """A staged chat attachment is processed only when the Agent invokes this tool."""
        with self._lock:
            self.get(owner, key)
            job = self._jobs[key]
            if job['status'] == 'analyzed':
                return copy.deepcopy(job['result']['observation'])
            if job['status'] != 'uploaded' or not job['image']:
                raise StylingError('photo_not_ready', '照片正在分析或已失效，请稍后重试', 409)
            job.update(status='running', message='数字客服正在本机观察服装配色与轮廓')
        # Share the single vision worker with the original page (no duplicate models).
        try:
            observation = self._executor.submit(self.vision.analyze, job['image'], job['mode']).result()
            observation['mode'] = job['mode']
            with self._lock:
                job.update(status='analyzed', result={'observation': observation}, message='看图完成', expires_at=self.clock() + TTL)
            return copy.deepcopy(observation)
        except Exception as exc:
            with self._lock:
                job.update(status='failed', error=getattr(exc, 'code', 'styling_failed'), message='照片分析失败')
            if isinstance(exc, StylingError):
                raise
            raise StylingError('vision_unavailable', '本地看图失败，可以继续用文字描述衣服') from exc
        finally:
            with self._lock:
                job['image'] = None

    def _run(self, key):
        with self._lock:
            job = self._jobs[key]
            job["status"], job["message"] = "running", "正在本地看图并检索商品，CPU首次处理可能需要数分钟"
        try:
            observation = self.vision.analyze(job["image"], job["mode"]) if job["image"] else None
            if observation:
                observation["mode"] = job["mode"]
            result = self.engine.plan(job["options"], observation)
            with self._lock:
                job.update(status="completed", result=result, message=result["summary"], expires_at=self.clock() + TTL, revision=secrets.token_hex(8))
        except Exception as exc:
            with self._lock:
                job.update(status="failed", error=getattr(exc, "code", "styling_failed"),
                           message=str(exc) if isinstance(exc, StylingError) else "搭配处理失败，请稍后重试或使用文字推荐")
        finally:
            # Source bytes are unnecessary after observation; delete even on error.
            with self._lock:
                job["image"] = None

    def get(self, owner, key):
        with self._lock:
            self._cleanup()
            job = self._jobs.get(key)
            if not job or job["owner"] != owner:
                raise StylingError("style_not_found", "搭配已过期或不属于当前会话，请重新推荐", 404)
            return self.view(job)

    def delete(self, owner, key):
        with self._lock:
            self.get(owner, key)
            if self._jobs[key]["status"] in {"queued", "running"}:
                raise StylingError("style_active", "正在处理的照片会在任务结束后自动清除，请稍后删除建议", 409)
            del self._jobs[key]

    def refine(self, owner, key, payload):
        with self._lock:
            job = self.get(owner, key)
            if job["status"] != "completed":
                raise StylingError("style_not_complete", "请等待搭配分析完成", 409)
            result = job["result"]
            incoming = payload.get("options", {})
            base = {**result["options"], **clean_preferences(incoming)}
            # Removing a supplied measurement/temperature clears its old value too.
            for field in ("height_cm", "weight_kg", "temperature"):
                if field in incoming and incoming[field] in (None, ""):
                    base.pop(field, None)
            options = parse_feedback(payload.get("feedback", ""), base)
            keep = payload.get("keep_product_ids", [])
            exclude = payload.get("exclude_product_ids", [])
            if not isinstance(keep, list) or not isinstance(exclude, list) or any(not isinstance(i, str) for i in keep + exclude):
                raise StylingError("invalid_selection", "请选择有效商品")
            if "保留" in str(payload.get("feedback", "")) and not keep and result["outfits"]:
                target = "bottoms" if "裤" in payload["feedback"] else "outerwear" if "外套" in payload["feedback"] else "tops"
                keep = [p["product_id"] for p in result["outfits"][0]["items"] if p["group"] == target]
            # Relative price feedback is a constraint on actual alternatives,
            # not permission to silently replace the customer's stated budget.
            from .workflow import item_edits, CHEAPER
            feedback = str(payload.get('feedback') or '')[:500]
            cheaper = item_edits(feedback)[2]
            items = result['outfits'][0]['items'] if result['outfits'] else []
            caps = {item['group']: round(item['price'] - .01, 2) for item in items if item['group'] in cheaper}
            if cheaper:
                changed_ids = {item['product_id'] for item in items if item['group'] in cheaper}
                keep = list(dict.fromkeys([pid for pid in keep if pid not in changed_ids] +
                            [item['product_id'] for item in items if item['group'] not in cheaper]))
            total_cap = (round(result['outfits'][0]['total'] - .01, 2)
                         if result['outfits'] and re.search(CHEAPER, feedback) and not cheaper else None)
            updated = self.engine.plan(options, result.get("observation"), keep, exclude,
                                       max_item_prices=caps, max_total_price=total_cap)
            self._jobs[key].update(result=updated, message=updated["summary"], expires_at=self.clock() + TTL, revision=secrets.token_hex(8))
            return self.view(self._jobs[key])

    def latest(self, owner):
        with self._lock:
            self._cleanup()
            jobs = [j for j in self._jobs.values() if j["owner"] == owner and j["status"] == "completed"]
            return self.view(jobs[-1]) if jobs else None

    def remember_plan(self, owner, result):
        """Keep text recommendations too, so conversational refinement has context."""
        if not owner:
            return
        with self._lock:
            self._cleanup()
            if len(self._jobs) >= 32:
                completed = [key for key, value in self._jobs.items() if value["status"] in {"completed", "failed"}]
                if not completed:
                    raise StylingError("style_busy", "穿搭服务忙，请稍后再试", 429)
                del self._jobs[completed[0]]
            key = secrets.token_hex(16)
            self._jobs[key] = {"job_id": key, "owner": owner, "mode": "text", "status": "completed",
                               "image": None, "options": result["options"], "result": result,
                               "message": result["summary"], "expires_at": self.clock() + TTL, "revision": secrets.token_hex(8), "error": None}


_service = None
_lock = threading.Lock()


def get_styling_service():
    global _service
    with _lock:
        if _service is None:
            _service = StylingService()
        return _service
