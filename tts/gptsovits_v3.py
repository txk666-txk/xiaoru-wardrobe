import os
import time
import wave

import requests

from utils import config_util as cfg
from utils import util


class Speech:

    def __init__(self):
        self.api_url = getattr(cfg, "gptsovits_api_url", None) or "http://127.0.0.1:9880/tts"
        self.ref_audio_path = getattr(cfg, "gptsovits_ref_audio_path", None) or ""
        self.prompt_text = getattr(cfg, "gptsovits_prompt_text", None) or ""
        self.prompt_lang = getattr(cfg, "gptsovits_prompt_lang", None) or "zh"
        self.text_lang = getattr(cfg, "gptsovits_text_lang", None) or "zh"
        self.timeout = float(getattr(cfg, "gptsovits_timeout", None) or 120)

    def connect(self):
        pass

   
    def close(self):
       pass

    def to_sample(self, text, style):
        if not os.path.isfile(self.ref_audio_path):
            util.log(1, "[x] GPT-SoVITS 参考音频不存在: " + self.ref_audio_path)
            return None

        data = {
            "text": text,
            "text_lang": self.text_lang,
            "ref_audio_path": self.ref_audio_path,
            "prompt_text": self.prompt_text,
            "prompt_lang": self.prompt_lang,
            "top_k": 5,
            "top_p": 1,
            "temperature": 1,
            "text_split_method": "cut5",
            "batch_size": 1,
            "batch_threshold": 0.75,
            "split_bucket": True,
            "speed_factor": 1.0,
            "fragment_interval": 0.3,
            "seed": -1,
            "media_type": "wav",
            "streaming_mode": False,
            "parallel_infer": True,
            "repetition_penalty": 1.35,
        }
        file_url = None
        try:
            response = requests.post(
                self.api_url,
                json=data,
                timeout=(5, self.timeout),
            )
            response.raise_for_status()
            if not response.content.startswith(b"RIFF"):
                raise ValueError("GPT-SoVITS 未返回有效 WAV 音频")

            os.makedirs("./samples", exist_ok=True)
            file_url = "./samples/sample-" + str(int(time.time() * 1000)) + ".wav"
            # api_v2 返回的是完整 WAV 文件，必须直接保存，不能再次封装 WAV 头。
            with open(file_url, "wb") as audio_file:
                audio_file.write(response.content)

            with wave.open(file_url, "rb") as wav_file:
                if wav_file.getnframes() <= 0:
                    raise ValueError("GPT-SoVITS 返回的 WAV 没有音频帧")
            return file_url
        except Exception as exc:
            if file_url and os.path.exists(file_url):
                os.remove(file_url)
            util.log(1, "[x] GPT-SoVITS 语音转换失败！")
            util.log(1, "[x] 原因: " + str(exc))
            return None
