from __future__ import annotations

import asyncio
import json
import subprocess
import time
import wave
from pathlib import Path

from app.tts.schemas import SynthesisRequest
from app.process_supervisor import register_process, unregister_process
from app.database import rows

from .base import TTSProvider, TTSProviderError


class WindowsTTSProvider(TTSProvider):
    id = "windows"
    name = "Windows System TTS"
    version = "SAPI.SpVoice"
    device = "cpu"

    def __init__(self) -> None:
        self._active: dict[str, asyncio.subprocess.Process] = {}
        self._metrics = {"requests": 0, "failures": 0, "cancelled": 0, "last_synthesis_ms": None}

    async def health_check(self) -> dict:
        try:
            voices = await self.list_voices()
            status, error = ("ok", None) if voices else ("unavailable", "WINDOWS_TTS_NO_VOICES")
        except TTSProviderError as exc:
            voices, status, error = [], "unavailable", exc.code
        return {"provider": self.id, "status": status, "device": self.device, "voices": len(voices), "version": self.version, "error_code": error}

    async def list_voices(self) -> list[dict]:
        try:
            return await asyncio.to_thread(_windows_voices)
        except Exception as exc:
            raise TTSProviderError(f"Windows SAPI voice enumeration failed: {type(exc).__name__}", "TTS_PROVIDER_UNAVAILABLE") from exc

    async def synthesize(self, request: SynthesisRequest, output: Path) -> dict:
        output.parent.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        process: asyncio.subprocess.Process | None = None
        try:
            if len(request.text) > 240:
                raise TTSProviderError("Windows fallback accepts at most 240 characters per sentence", "TTS_INVALID_TEXT")
            rate = max(-10, min(10, round((request.speed - 1.0) * 8)))
            volume = max(0, min(100, round(request.volume * 100)))
            script = (
                "Add-Type -AssemblyName System.Speech;"
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                "$text=[Console]::In.ReadToEnd();"
                "$voice=$env:SIYI_TTS_VOICE;if($voice){$s.SelectVoice($voice)};"
                f"$s.Rate={rate};$s.Volume={volume};"
                "$s.SetOutputToWaveFile($env:SIYI_TTS_OUTPUT);$s.Speak($text);$s.Dispose()"
            )
            environment = dict(__import__("os").environ)
            environment["SIYI_TTS_OUTPUT"] = str(output)
            environment["SIYI_TTS_VOICE"] = request.voice
            process = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._active[request.request_id] = process
            try:
                supervised_task = request.task_id if request.task_id and rows("SELECT id FROM agent_tasks WHERE id=?", (request.task_id,)) else None
                register_process(process.pid, supervised_task, "powershell", ["System.Speech"], process)
            except Exception:
                process.terminate()
                await process.wait()
                raise
            _, error = await process.communicate(request.text.encode("utf-8"))
            if process.returncode != 0:
                raise TTSProviderError(
                    f"Windows TTS process failed with exit code {process.returncode}: {error.decode('utf-8', errors='replace')[:160]}",
                    "TTS_SYNTHESIS_FAILED",
                )
            if not output.is_file() or output.stat().st_size <= 46:
                raise TTSProviderError("Windows TTS produced no audible frames", "TTS_SYNTHESIS_FAILED")
            duration_ms, sample_rate = _wav_info(output)
            if duration_ms <= 0:
                raise TTSProviderError("Windows TTS produced a zero-duration WAV", "TTS_SYNTHESIS_FAILED")
            elapsed = round((time.perf_counter() - started) * 1000, 3)
            self._metrics.update({"requests": self._metrics["requests"] + 1, "last_synthesis_ms": elapsed})
            return {"duration_ms": duration_ms, "sample_rate": sample_rate, "synthesis_ms": elapsed}
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.terminate()
                await process.wait()
            output.unlink(missing_ok=True)
            self._metrics["cancelled"] += 1
            raise
        except TTSProviderError:
            self._metrics["failures"] += 1
            raise
        except Exception as exc:
            self._metrics["failures"] += 1
            output.unlink(missing_ok=True)
            raise TTSProviderError(f"Windows TTS synthesis failed: {type(exc).__name__}", "TTS_SYNTHESIS_FAILED") from exc
        finally:
            self._active.pop(request.request_id, None)
            if process is not None:
                unregister_process(process.pid, "tts_exited")

    async def cancel(self, request_id: str) -> bool:
        event = self._active.get(request_id)
        if not event or event.returncode is not None:
            return False
        event.terminate()
        self._metrics["cancelled"] += 1
        return True

    async def unload(self) -> dict:
        for process in self._active.values():
            if process.returncode is None:
                process.terminate()
        return {"provider": self.id, "status": "unloaded", "device": self.device}

    def get_status(self) -> dict:
        return {"provider": self.id, "active_requests": list(self._active), "device": self.device}

    def get_metrics(self) -> dict:
        return dict(self._metrics)


def _windows_voices() -> list[dict]:
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "@($s.GetInstalledVoices()|ForEach-Object{@{name=$_.VoiceInfo.Name;culture=$_.VoiceInfo.Culture.Name;enabled=$_.Enabled}})"
        "|ConvertTo-Json -Compress;$s.Dispose()"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    payload = json.loads(result.stdout)
    records = payload if isinstance(payload, list) else [payload]
    return [
        {"name": str(item.get("name") or ""), "culture": str(item.get("culture") or ""), "enabled": bool(item.get("enabled", True))}
        for item in records if isinstance(item, dict) and item.get("name")
    ]


def _wav_info(path: Path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        duration = round(handle.getnframes() / max(1, rate) * 1000)
        return duration, rate
