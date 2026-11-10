from __future__ import annotations

import base64
import os
from urllib.parse import urlparse

import requests

from .vision_contract import (COLORS, CATEGORIES, STYLES, PROMPT_VARIANTS,
                              PROMPT_VERSION, build_vision_prompt, parse_garments)


class StylingError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


class LocalVision:
    """Local observation adapter shared by the digital human and benchmarks.

    Ollama retains the CPU-only default. A local OpenAI-compatible server may
    load the original or adapted model independently, without changing chat or
    Agent tools. Its device and adapter are managed by that server.
    """

    def __init__(self, *, backend=None, origin=None, model=None, prompt_variant=None):
        self.backend = backend or os.environ.get("FAY_STYLE_VISION_BACKEND", "ollama")
        if self.backend not in {"ollama", "openai_local"}:
            raise StylingError("invalid_vision_backend", "看图后端必须为 ollama 或 openai_local")
        if self.backend == "ollama":
            default_origin = os.environ.get("FAY_STYLE_OLLAMA_URL", "http://127.0.0.1:11434")
        else:
            default_origin = os.environ.get("FAY_STYLE_VISION_URL", "http://127.0.0.1:8001/v1")
        self.origin = (origin or default_origin).rstrip("/")
        try:
            parsed = urlparse(self.origin)
            valid_path = parsed.path in {"", "/v1"} if self.backend == "openai_local" else not parsed.path
            valid = (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                     and not parsed.username and not parsed.password and valid_path
                     and not parsed.query and not parsed.fragment and parsed.port != 0)
        except ValueError:
            valid = False
        if not valid:
            raise StylingError("invalid_vision_origin", "看图服务必须使用本机 HTTP 地址，兼容后端可带 /v1")
        self.model = model or os.environ.get("FAY_STYLE_VISION_MODEL", "qwen3-vl:2b-instruct")
        self.prompt_variant = prompt_variant or os.environ.get("FAY_STYLE_VISION_PROMPT", "domain")
        if self.prompt_variant not in PROMPT_VARIANTS:
            raise StylingError("invalid_vision_prompt", "看图提示词必须为 baseline 或 domain")
        self.http = requests.Session()
        self.http.trust_env = False

    def metadata(self):
        return {"model": self.model, "backend": self.backend,
                "prompt_variant": self.prompt_variant, "prompt_version": PROMPT_VERSION,
                "device": "CPU" if self.backend == "ollama" else "由本机模型服务配置"}

    def status(self):
        try:
            path = "/api/tags" if self.backend == "ollama" else "/models"
            response = self.http.get(self.origin + path, timeout=3, allow_redirects=False)
            response.raise_for_status()
            payload = response.json()
            if self.backend == "ollama":
                models = [item.get("name") for item in payload.get("models", [])]
            else:
                models = [item.get("id") for item in payload.get("data", [])]
            ready = self.model in models
            missing = ("请运行 scripts/setup_styling.ps1 下载看图模型" if self.backend == "ollama"
                       else "本机视觉服务未加载指定模型，请核对 FAY_STYLE_VISION_MODEL")
            return {**self.metadata(), "available": ready,
                    "message": "本地看图模型就绪" if ready else missing}
        except (requests.RequestException, ValueError, AttributeError, TypeError):
            return {**self.metadata(), "available": False,
                    "message": "本机看图服务未就绪；文字穿搭和商品比较仍可使用"}

    def observe(self, image_bytes, mode, *, strict=False):
        """Observe without fallback; an empty garment list is valid for negatives."""
        if mode not in {"person", "garment"}:
            raise StylingError("invalid_mode", "看图模式必须为 person 或 garment")
        prompt = build_vision_prompt(mode, self.prompt_variant)
        encoded = base64.b64encode(image_bytes).decode("ascii")
        if self.backend == "ollama":
            path = "/api/chat"
            body = {
                "model": self.model, "stream": False, "think": False, "format": "json", "keep_alive": 0,
                "messages": [{"role": "user", "content": prompt, "images": [encoded]}],
                "options": {"num_gpu": 0, "num_ctx": 4096, "num_predict": 512, "temperature": 0},
            }
        else:
            path = "/chat/completions"
            # Chat uploads are normalized to JPEG before reaching this adapter.
            # The benchmark uses the same normalization path.
            body = {"model": self.model, "stream": False, "temperature": 0, "max_tokens": 512,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded}},
                    ]}]}
        try:
            response = self.http.post(self.origin + path, json=body,
                                      timeout=(5, 240), allow_redirects=False)
            if response.status_code == 404:
                raise StylingError("vision_missing", "本机看图模型或接口不存在，请检查服务与模型名称", 503)
            response.raise_for_status()
            payload = response.json()
            raw = (payload["message"]["content"] if self.backend == "ollama"
                   else payload["choices"][0]["message"]["content"])
            result = parse_garments(raw, strict=strict)
            return {"garments": result, "source": "local_vision", **self.metadata(),
                    "notice": "照片识别可能有误，可在下方调整风格或重新上传；尺码需填写真实数据。"}
        except StylingError:
            raise
        except requests.Timeout as exc:
            raise StylingError("vision_timeout", "本地看图超时，建议换一张清晰照片，或先使用文字推荐", 503) from exc
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise StylingError("vision_unavailable", "本地看图未返回可用结果；可以先按用途、预算进行文字推荐", 503) from exc

    def analyze(self, image_bytes, mode):
        result = self.observe(image_bytes, mode)
        if not result["garments"]:
            raise StylingError("unclear_photo", "没有辨认出清晰服装，请换一张光线充足、衣服无遮挡的照片")
        return result
