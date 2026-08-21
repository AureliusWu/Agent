"""Private local STT worker entry point.

The sidecar starts this module through its own executable so a user stop can
terminate a real inference process instead of merely cancelling an asyncio
wrapper around a non-interruptible native call.  Stdout is a JSON-only private
pipe; it is never forwarded to normal logs or persisted with transcript text.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.stt.pcm_wav import install_pcm_wav_pyav_compat
from app.stt.providers.faster_whisper import FasterWhisperProvider
from app.stt.schemas import STTError, TranscriptionRequest


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _request(payload: dict[str, Any]) -> TranscriptionRequest:
    return TranscriptionRequest(
        request_id=str(payload["request_id"]),
        voice_session_id=str(payload["voice_session_id"]),
        audio_path=str(payload["audio_path"]),
        audio_sha256=str(payload["audio_sha256"]),
        audio_duration_ms=int(payload["audio_duration_ms"]),
        language=str(payload.get("language") or "zh"),
        model_id=str(payload["model_id"]),
        device=str(payload.get("device") or "cpu"),
        compute_type=str(payload.get("compute_type") or "int8"),
        vad=bool(payload.get("vad", True)),
        timestamps=bool(payload.get("timestamps", False)),
    )


def run_inference(*, model_id: str, model_path: Path, device: str, compute_type: str) -> int:
    provider = FasterWhisperProvider()
    try:
        ready = provider.load_model(model_id, model_path, device=device, compute_type=compute_type)
    except STTError as exc:
        _write({"event": "ready", "status": "error", "code": exc.code})
        return 2
    except Exception:
        _write({"event": "ready", "status": "error", "code": "STT_MODEL_LOAD_FAILED"})
        return 2

    _write({"event": "ready", "status": "ok", "model": model_id, "load_ms": ready.get("load_ms")})
    for line in sys.stdin:
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(line)
            if payload.get("operation") == "shutdown":
                _write({"event": "shutdown", "status": "ok"})
                return 0
            request = _request(payload)
            result = provider.transcribe(request)
            _write({"event": "result", "status": "ok", "request_id": request.request_id, "result": result.as_dict()})
        except STTError as exc:
            _write({"event": "result", "status": "error", "request_id": str(payload.get("request_id") or ""), "code": exc.code})
        except Exception:
            _write({"event": "result", "status": "error", "request_id": str(payload.get("request_id") or ""), "code": "STT_TRANSCRIPTION_FAILED"})
    return 0


def run_download(*, repo_id: str, target: Path) -> int:
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(repo_id=repo_id, local_dir=str(target))
        _write({"event": "download", "status": "ok"})
        return 0
    except Exception:
        _write({"event": "download", "status": "error", "code": "STT_DOWNLOAD_FAILED"})
        return 2


def run_probe() -> int:
    """Verify the frozen PCM-only STT runtime and VAD assets without a model."""
    try:
        import ctranslate2
        pyav_compat = install_pcm_wav_pyav_compat()
        import faster_whisper
        import onnxruntime
        import tokenizers
        from faster_whisper.vad import get_vad_model

        vad_model = get_vad_model()
        if not getattr(vad_model, "session", None):
            raise RuntimeError("VAD session did not initialize")

        _write(
            {
                "event": "probe",
                "status": "ok",
                "runtime": {
                    "faster_whisper": bool(faster_whisper),
                    "ctranslate2": bool(ctranslate2),
                    "pcm_wav_decoder": True,
                    "pyav_required": False,
                    "pyav_compat": pyav_compat,
                    "onnxruntime": bool(onnxruntime),
                    "tokenizers": bool(tokenizers),
                    "vad_session": True,
                },
            }
        )
        return 0
    except Exception:
        _write({"event": "probe", "status": "error", "code": "STT_RUNTIME_PROBE_FAILED"})
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", choices=("infer", "download", "probe"), required=True)
    parser.add_argument("--model-id")
    parser.add_argument("--model-path")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--repo-id")
    parser.add_argument("--target")
    args = parser.parse_args(argv)
    if args.mode == "probe":
        return run_probe()
    if args.mode == "infer":
        if not args.model_id or not args.model_path:
            return 2
        return run_inference(
            model_id=args.model_id,
            model_path=Path(args.model_path),
            device=args.device,
            compute_type=args.compute_type,
        )
    if not args.repo_id or not args.target:
        return 2
    return run_download(repo_id=args.repo_id, target=Path(args.target))


if __name__ == "__main__":
    raise SystemExit(main())
