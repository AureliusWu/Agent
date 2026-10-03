"""Speech tool orchestration; audio and text remain within the common task trace."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.config import settings
from app.plugins.contracts import PluginCall
from app.plugins.handlers import outcome, permission_arguments
from app.providers.speech import (
    SpeechProviderError,
    SynthesisRequest,
    TranscriptionRequest,
    speech_adapter,
    valid_audio,
)
from app.tools.outcomes import RuntimeToolOutcome


def _service_configured() -> bool:
    return bool(
        settings.speech_enabled
        and settings.speech_base_url.strip()
        and settings.speech_api_key.get_secret_value().strip()
    )


def transcription_configured() -> bool:
    return _service_configured() and bool(settings.speech_transcription_model.strip())


def synthesis_configured() -> bool:
    return _service_configured() and bool(
        settings.speech_synthesis_model.strip() and settings.speech_voice.strip()
    )


def _read_audio(path: Path) -> tuple[bytes, str]:
    types = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "m4a": "audio/mp4",
        "mp4": "audio/mp4",
        "ogg": "audio/ogg",
        "webm": "audio/webm",
    }
    suffix = path.suffix.lower().lstrip(".")
    if not path.is_file() or suffix not in types:
        raise SpeechProviderError("请选择支持的工作区音频文件")
    with path.open("rb") as stream:
        content = stream.read(settings.speech_max_audio_bytes + 1)
    if not content or len(content) > settings.speech_max_audio_bytes:
        raise SpeechProviderError("音频为空或超过输入限制")
    if not valid_audio(content, suffix):
        raise SpeechProviderError("音频文件头与扩展名不一致")
    return content, types[suffix]


async def execute_voice(call: PluginCall) -> RuntimeToolOutcome:
    from app.data_flow import record_data_flow
    from app.sandbox import (
        FileVersionError,
        SandboxError,
        file_version_token,
        safe_path,
        workspace_root,
        write_uploaded_file,
    )
    from app.security.network_security import NetworkPolicyError
    from app.security.trust import redact_payload, secure_untrusted_payload
    from app.workspace.snapshots import SnapshotError, create_security_snapshot
    from app.workspace.file_locks import FileLockConflict, acquire_file_locks, release_file_locks

    source = "plugin:voice"
    decision = call.permission_fn(
        **permission_arguments(call, "音频/文字将发送至语音服务；合成结果写入当前工作区"),
        source=source,
    )
    if not decision.allowed:
        return outcome(
            call,
            decision.confirmation or {"success": False, "status": "confirmation_required"},
            decision.confirmed,
            source,
        )
    message_id = str(call.arguments.get("message_id") or call.tool_call_id)
    lease = None
    try:
        root = workspace_root(call.workspace)
        path = safe_path(
            root, str(call.arguments["path"]), must_exist=call.name == "transcribe_audio"
        )
        if call.name == "transcribe_audio":
            content, media_type = _read_audio(path)
            record_data_flow(
                source="workspace_audio",
                sink="speech_provider",
                classification="internal",
                fields=("audio", "media_type"),
                allowed=True,
                reason="explicitly approved audio transcription",
                conversation_id=call.conversation_id,
                task_id=call.task_id,
            )
            result = await speech_adapter().transcribe(
                TranscriptionRequest(content, media_type, str(call.arguments.get("language") or ""))
            )
            secured, sensitive, findings = secure_untrusted_payload(
                {"text": result.text}, "speech:transcription"
            )
            record_data_flow(
                source="speech_provider",
                sink="agent_context",
                classification=sensitive.classification,
                fields=("transcript",),
                redactions=sensitive.redactions,
                allowed=True,
                reason="untrusted transcript",
                conversation_id=call.conversation_id,
                task_id=call.task_id,
            )
            data = {
                **secured,
                "path": str(path.relative_to(root)),
                "model": result.model,
                "message_id": message_id,
                "source_type": "audio",
                "security": {"findings": findings, "redactions": sensitive.redactions},
            }
        else:
            if not call.task_id:
                return outcome(
                    call,
                    {
                        "success": False,
                        "status": "error",
                        "error_code": "task_required",
                        "error_message": "语音合成需要关联任务，以保证写入锁、撤销和恢复可追溯",
                    },
                    decision.confirmed,
                    source,
                )
            audio_format = str(call.arguments.get("format") or "mp3")
            if (
                path == root
                or (path.exists() and not path.is_file())
                or path.suffix.lower() != f".{audio_format}"
            ):
                raise SpeechProviderError("合成目标必须为工作区内与所选格式一致的音频文件")
            expected = str(call.arguments["expected_version_token"])
            lease = acquire_file_locks(
                call.workspace,
                (str(path.relative_to(root)),),
                holder_task_id=call.task_id,
                holder_agent_id="plugin:voice",
            )
            if file_version_token(path) != expected:
                raise FileVersionError("version_conflict", "目标文件已变化，请重新读取版本后再合成")
            text = str(call.arguments["text"])
            if not text.strip():
                raise SpeechProviderError("语音合成文本不能为空")
            _, sensitive = redact_payload({"text": text})
            if sensitive.redactions:
                record_data_flow(
                    source="agent_context",
                    sink="speech_provider",
                    classification="credential",
                    fields=("text",),
                    redactions=sensitive.redactions,
                    allowed=False,
                    reason="credential-bearing speech text blocked",
                    conversation_id=call.conversation_id,
                    task_id=call.task_id,
                )
                return outcome(
                    call,
                    {
                        "success": False,
                        "status": "error",
                        "error_code": "credential_flow_blocked",
                        "error_message": "语音合成文本包含凭据，已阻止外发",
                    },
                    decision.confirmed,
                    source,
                )
            snapshot = create_security_snapshot(
                call.workspace,
                reason="before_speech:synthesis",
                conversation_id=call.conversation_id,
                task_id=call.task_id,
            )
            record_data_flow(
                source="agent_context",
                sink="speech_provider",
                classification="internal",
                fields=("text", "format"),
                allowed=True,
                reason="explicitly approved speech synthesis",
                conversation_id=call.conversation_id,
                task_id=call.task_id,
            )
            result = await speech_adapter().synthesize(SynthesisRequest(text, audio_format))
            if (
                not result.audio
                or len(result.audio) > settings.speech_max_audio_bytes
                or not valid_audio(result.audio, audio_format)
            ):
                raise SpeechProviderError("语音适配器返回的音频无效或超过限制")
            # Cancellation is observed before the synchronous atomic commit.
            await asyncio.sleep(0)
            saved = write_uploaded_file(
                call.workspace,
                "full",
                str(call.arguments["path"]),
                result.audio,
                conversation_id=call.conversation_id,
                task_id=call.task_id,
                expected_version_token=expected,
                tool_call_id=call.tool_call_id,
                operation="synthesize_speech",
                permission_fn=call.permission_fn,
            )
            if not saved.get("success"):
                return outcome(call, saved, decision.confirmed, source)
            data = {
                **saved["data"],
                "model": result.model,
                "voice": result.voice,
                "media_type": result.media_type,
                "message_id": message_id,
                "source_type": "text",
                "ai_generated": True,
                "security_snapshot_id": snapshot["id"],
            }
        return outcome(
            call,
            {"success": True, "status": "ok", "data": data, **data},
            decision.confirmed,
            source,
        )
    except FileLockConflict:
        return outcome(
            call,
            {
                "success": False,
                "status": "error",
                "error_code": "file_lock_conflict",
                "error_message": "合成目标正在被其他任务修改",
                "retryable": True,
            },
            decision.confirmed,
            source,
        )
    except FileVersionError as exc:
        return outcome(
            call,
            {
                "success": False,
                "status": "error",
                "error_code": exc.code,
                "error_message": str(exc),
                "retryable": True,
            },
            decision.confirmed,
            source,
        )
    except (
        SpeechProviderError,
        SandboxError,
        SnapshotError,
        NetworkPolicyError,
        httpx.HTTPError,
        OSError,
        ValueError,
    ) as exc:
        # Provider bodies, endpoints and secret-bearing exception strings stay out of traces.
        error = (
            str(exc)
            if isinstance(exc, (SpeechProviderError, SandboxError, FileVersionError))
            else "语音服务调用失败；请检查服务配置、网络或供应商状态"
        )
        return outcome(
            call,
            {
                "success": False,
                "status": "error",
                "error_code": "speech_failed",
                "error_message": error,
                "retryable": isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)),
            },
            decision.confirmed,
            source,
        )
    finally:
        release_file_locks(lease)
