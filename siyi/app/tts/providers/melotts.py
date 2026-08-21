from __future__ import annotations

import asyncio
import importlib.util
import os
import time
from pathlib import Path

from app.tts.schemas import SynthesisRequest

from .base import TTSProvider, TTSProviderError
from .windows_tts import _wav_info


class MeloTTSProvider(TTSProvider):
    id = "melotts"
    name = "MeloTTS"
    version = "optional-local"
    device = "cpu"
    maturity = "experimental"

    def __init__(self) -> None:
        self._model = None
        self._active: set[str] = set()
        self._metrics = {"requests": 0, "failures": 0, "last_synthesis_ms": None, "model_loaded": False}

    def _available(self) -> tuple[bool, str | None]:
        if importlib.util.find_spec("melo") is None:
            return False, "MELOTTS_PACKAGE_MISSING"
        if os.environ.get("SIYI_MELOTTS_ALLOW_MODEL_DOWNLOAD") != "1" and not os.environ.get("SIYI_MELOTTS_MODEL_READY"):
            return False, "MELOTTS_MODEL_NOT_CONFIRMED"
        return True, None

    async def health_check(self) -> dict:
        available, error = self._available()
        return {"provider": self.id, "status": "ok" if available else "unavailable", "device": self.device, "maturity": self.maturity, "lazy_loaded": self._model is None, "error_code": error}

    async def list_voices(self) -> list[dict]:
        available, _ = self._available()
        return [{"name": "ZH", "culture": "zh-CN", "enabled": True}] if available else []

    def _load(self):
        available, error = self._available()
        if not available:
            raise TTSProviderError("MeloTTS is unavailable or its model download was not confirmed", error or "TTS_PROVIDER_UNAVAILABLE")
        if self._model is None:
            from melo.api import TTS
            self._model = TTS(language="ZH", device="cpu")
            self._metrics["model_loaded"] = True
        return self._model

    async def synthesize(self, request: SynthesisRequest, output: Path) -> dict:
        self._active.add(request.request_id)
        started = time.perf_counter()
        try:
            model = await asyncio.to_thread(self._load)
            speakers = getattr(getattr(getattr(model, "hps", None), "data", None), "spk2id", {})
            speaker = speakers.get(request.voice) or speakers.get("ZH") or next(iter(speakers.values()), 0)
            await asyncio.to_thread(
                lambda: model.tts_to_file(request.text, speaker, str(output), speed=request.speed)
            )
            duration_ms, sample_rate = _wav_info(output)
            elapsed = round((time.perf_counter() - started) * 1000, 3)
            self._metrics.update({"requests": self._metrics["requests"] + 1, "last_synthesis_ms": elapsed})
            return {"duration_ms": duration_ms, "sample_rate": sample_rate, "synthesis_ms": elapsed}
        except TTSProviderError:
            self._metrics["failures"] += 1
            raise
        except Exception as exc:
            self._metrics["failures"] += 1
            raise TTSProviderError(f"MeloTTS synthesis failed: {type(exc).__name__}", "TTS_SYNTHESIS_FAILED") from exc
        finally:
            self._active.discard(request.request_id)

    async def cancel(self, request_id: str) -> bool:
        return request_id in self._active

    async def unload(self) -> dict:
        self._model = None
        self._metrics["model_loaded"] = False
        return {"provider": self.id, "status": "unloaded", "device": self.device}

    def get_status(self) -> dict:
        return {
            "provider": self.id,
            "active_requests": sorted(self._active),
            "device": self.device,
            "loaded": self._model is not None,
            "maturity": self.maturity,
        }

    def get_metrics(self) -> dict:
        return dict(self._metrics)
