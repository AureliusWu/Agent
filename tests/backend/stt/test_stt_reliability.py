from __future__ import annotations

from app.stt.providers.faster_whisper import _transcription_reliability
from app.stt.schemas import MIN_AUTO_SEND_CONFIDENCE, TranscriptionResult


def _result(**changes: object) -> TranscriptionResult:
    values: dict[str, object] = {
        "request_id": "request",
        "provider": "faster_whisper",
        "model": "small",
        "language": "zh",
        "text": "可以发送",
        "segments": [],
        "duration_ms": 1000,
        "transcription_ms": 200.0,
        "real_time_factor": 0.2,
    }
    values.update(changes)
    return TranscriptionResult(**values)  # type: ignore[arg-type]


def test_faster_whisper_reliability_requires_all_explicit_scores() -> None:
    confidence, reliable, reason = _transcription_reliability(
        language_probability=0.98,
        recognized_segment_count=2,
        samples=[(-0.10, 0.02), (-0.20, 0.05)],
    )

    assert confidence is not None and confidence >= MIN_AUTO_SEND_CONFIDENCE
    assert reliable is True
    assert reason == "RELIABLE"

    assert _transcription_reliability(
        language_probability=0.98,
        recognized_segment_count=2,
        samples=[(-0.10, 0.02)],
    ) == (None, False, "CONFIDENCE_UNAVAILABLE")
    assert _transcription_reliability(
        language_probability=float("nan"),
        recognized_segment_count=1,
        samples=[(-0.10, 0.02)],
    ) == (None, False, "CONFIDENCE_UNAVAILABLE")


def test_low_confidence_and_legacy_results_fail_closed_for_auto_send() -> None:
    confidence, reliable, reason = _transcription_reliability(
        language_probability=0.95,
        recognized_segment_count=1,
        samples=[(-1.20, 0.10)],
    )
    assert confidence is not None and confidence < MIN_AUTO_SEND_CONFIDENCE
    assert reliable is False
    assert reason == "LOW_CONFIDENCE"

    assert _result(
        confidence=0.91,
        reliable=True,
        reliability_reason="RELIABLE",
    ).auto_send_safe is True
    assert _result(
        confidence=confidence,
        reliable=reliable,
        reliability_reason=reason,
    ).auto_send_safe is False
    assert _result().auto_send_safe is False
    assert _result(confidence=0.91, reliable=True).auto_send_safe is False
    assert _result(confidence=float("inf"), reliable=True, reliability_reason="RELIABLE").auto_send_safe is False
    assert _result(text="   ", confidence=0.91, reliable=True, reliability_reason="RELIABLE").auto_send_safe is False
