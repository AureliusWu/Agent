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
    maturity = "stable"

    def __init__(self) -> None:
        self._active: dict[str, asyncio.subprocess.Process] = {}
        self._metrics = {"requests": 0, "failures": 0, "cancelled": 0, "last_synthesis_ms": None}

    @staticmethod
    async def _terminate_and_wait(
        process: asyncio.subprocess.Process,
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        """Stop one owned SAPI child and do not return while its PID is live."""

        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            return
        except asyncio.TimeoutError:
            pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()

    async def health_check(self) -> dict:
        try:
            voices = await self.list_voices()
            status, error = ("ok", None) if voices else ("unavailable", "WINDOWS_TTS_NO_VOICES")
        except TTSProviderError as exc:
            voices, status, error = [], "unavailable", exc.code
        return {"provider": self.id, "status": status, "device": self.device, "voices": len(voices), "version": self.version, "maturity": self.maturity, "error_code": error}

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
                await self._terminate_and_wait(process)
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
        await self._terminate_and_wait(event)
        self._metrics["cancelled"] += 1
        return event.returncode is not None

    async def unload(self) -> dict:
        processes = list(self._active.values())
        await asyncio.gather(
            *(self._terminate_and_wait(process) for process in processes),
            return_exceptions=False,
        )
        return {
            "provider": self.id,
            "status": "unloaded" if all(process.returncode is not None for process in processes) else "unload_failed",
            "device": self.device,
        }

    def get_status(self) -> dict:
        return {
            "provider": self.id,
            "active_requests": list(self._active),
            "active_pids": sorted(
                process.pid
                for process in self._active.values()
                if process.returncode is None
            ),
            "device": self.device,
            "maturity": self.maturity,
        }

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
