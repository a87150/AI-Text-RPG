# -*- coding: utf-8 -*-
"""Qwen3-TTS \u7684\u4e09\u4e2a\u6743\u91cd\u5bf9\u5e94\u4e09\u79cd\u7528\u6cd5\uff1a

    design  VoiceDesign  \u7528\u4e00\u53e5\u8bdd\u63cf\u8ff0\u97f3\u8272
    custom  CustomVoice  \u9009\u4e00\u4e2a\u5185\u7f6e\u8bf4\u8bdd\u4eba\uff0c\u53ef\u4ee5\u518d\u52a0\u8bed\u6c14\u63cf\u8ff0
    clone   Base         \u62ff\u4e00\u6bb5\u53c2\u8003\u97f3\u9891\u590d\u523b\u97f3\u8272

\u6743\u91cd\u5f88\u5927\uff081.7B\uff09\uff0c\u6240\u4ee5 load() \u624d\u771f\u6b63\u5411\u4e0b\u62c9\uff0c\u6784\u9020\u65f6\u4e0d\u78b0\u4efb\u4f55\u4e1c\u897f\u3002
"""
from __future__ import annotations

import io
import wave

from voco.core.base_model import BaseAudioModel

WEIGHTS = {
    "design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "custom": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "clone": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
}

_TORCH_DTYPES = {"float32": "float32", "float16": "float16", "bfloat16": "bfloat16"}


def _wav_bytes(samples, rate):
    """\u6ce2\u5f62\u6570\u7ec4 -> 16bit \u5355\u58f0\u9053 wav\u3002voco \u53ea\u6709\u62ff\u5230 bytes \u624d\u4f1a\u5b58\u7f13\u5b58\u3002"""
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("\u7f3a\u5c11 numpy\uff0c\u6ca1\u6cd5\u628a\u6ce2\u5f62\u5c01\u6210 wav") from exc
    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(int(rate) or 24000)
        f.writeframes(pcm)
    return buf.getvalue()


class Qwen3TTS(BaseAudioModel):
    def __init__(self, device: str = "cpu", dtype: str = "float32", **kwargs) -> None:
        super().__init__(device=device, dtype=dtype, **kwargs)
        self.variant = str(kwargs.get("variant") or "custom").lower()
        if self.variant not in WEIGHTS:
            self.variant = "custom"
        self.model_id = kwargs.get("model_id") or WEIGHTS[self.variant]
        self._model = None

    def load(self) -> None:
        import torch
        from qwen_tts import Qwen3TTSModel

        dt = getattr(torch, _TORCH_DTYPES.get(str(self.dtype), "bfloat16"))
        # flash_attention_2 \u5728 Windows \u4e0a\u88c5\u4e0d\u4e0a\uff0c\u4ea4\u7ed9 transformers \u81ea\u5df1\u9009 sdpa
        self._model = Qwen3TTSModel.from_pretrained(
            self.model_id,
            device_map=self.device,
            dtype=dt,
            # 不显式传的话 transformers 会落到 eager，1.7B 上一句话要跑好几分钟
            attn_implementation=str(self.config.get("attn") or "sdpa"),
        )
        self._loaded = True

    def unload(self) -> None:
        self._model = None
        super().unload()

    def generate(self, text: str = "", **kwargs):
        if not self._model:
            self.load()
        body = str(text or "").strip()
        if not body:
            raise ValueError("\u6ca1\u6709\u8981\u5ff5\u7684\u6587\u5b57")

        lang = kwargs.get("language") or "Auto"
        instruct = kwargs.get("instruct") or ""

        if self.variant == "design":
            wavs, sr = self._model.generate_voice_design(
                text=body, language=lang, instruct=instruct)
        elif self.variant == "clone":
            ref = kwargs.get("ref_audio")
            if not ref:
                raise ValueError("clone \u6a21\u5f0f\u8981\u7ed9 ref_audio\uff08\u53c2\u8003\u97f3\u9891\u8def\u5f84\u6216 URL\uff09")
            wavs, sr = self._model.generate_voice_clone(
                text=body, language=lang, ref_audio=ref,
                ref_text=kwargs.get("ref_text") or "")
        else:
            wavs, sr = self._model.generate_custom_voice(
                text=body, language=lang,
                speaker=kwargs.get("speaker") or "Vivian",
                instruct=instruct)

        return _wav_bytes(wavs[0], sr)
