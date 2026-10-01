# 司忆 v14.0.0 实施反馈

本文件由 `scripts/v14-evidence.py` 根据当前证据台账生成。它不执行测试，也不会将代码审查或接口存在写为 PASS。

## 基本信息

- 目标版本：`14.0.0`
- 当前 VERSION：`14.0.0`
- Git HEAD：`653f61efadc87c7afa053453be5549402657a151`
- 工作树已清洁核验：`True`
- 计划：`司忆%20v14.0.0%20实施计划.md`
- 计划 SHA-256：`6BE5204C439A9FB3611BB6D6B6F93FB543EFADCC690940AE45DA36C73B8B2F28`
- 计划总行数：`2218`

## Git 状态

- source_commit=`653f61efadc87c7afa053453be5549402657a151`；workspace_clean=`True`；source_tree_fingerprint=`014B8C551D7DAAB01E7654F1CA292E24291CE244058A3524E85FCF1C5A8B9C6E`。

## 版本与 Schema

- 目标版本=`14.0.0`；当前 VERSION=`14.0.0`；SQLite Schema=`42`。

## 原始目标

- 发布门禁清零与语音输出正式化。
- 本地 STT、录音和语音输入闭环。
- 全链路语音交互、资源协调与稳定性验证。

## 发布判定

- 当前状态：`BLOCKED`
- 判定依据：安全或隐私红线尚未明确通过。
- 安全红线：`NOT_RUN`；隐私红线：`NOT_RUN`

## 逐项完成情况（A01–A28）

| 编号 | 能力 | 状态 | 说明 |
|---|---|---|---|
| A01 | 基线核验 | PASS | 已记录实际执行证据。 |
| A02 | TTS 正式化 | PASS | 已记录实际执行证据。 |
| A03 | 证据目录 | PASS | 已记录实际执行证据。 |
| A04 | 麦克风权限 | NOT_RUN | Physical microphone permission denial and grant have not been executed on the current candidate. |
| A05 | 设备管理 | NOT_RUN | Physical device enumeration, switching, disconnect, and busy-device flows have not been executed on the current candidate. |
| A06 | 录音 | NOT_RUN | Physical recording start, stop, cancel, and format validation have not been executed on the current candidate. |
| A07 | 临时音频 | PASS | 已记录实际执行证据。 |
| A08 | STT Provider | PASS | 已记录实际执行证据。 |
| A09 | STT 模型管理 | PASS | 已记录实际执行证据。 |
| A10 | CPU 默认策略 | PASS | 已记录实际执行证据。 |
| A11 | STT HTTP API | PASS | 已记录实际执行证据。 |
| A12 | Voice Events | NOT_RUN | Desktop Voice Events ordering has not been verified in the current desktop flow. |
| A13 | 转写编辑 | NOT_RUN | Transcript edit and cancellation have not been verified in the current desktop flow. |
| A14 | 消息接入 | NOT_RUN | Manual and automatic send routing have not been verified in the current desktop flow. |
| A15 | 权限安全 | NOT_RUN | Dangerous voice-task confirmation and rejection have not been verified in the current desktop flow. |
| A16 | TTS 打断 | NOT_RUN | Recording-triggered TTS interruption has not been verified in the current desktop flow. |
| A17 | 全局停止 | NOT_RUN | Four-stage global stop and idempotency have not been verified in the current desktop flow. |
| A18 | 半双工 | NOT_RUN | Half-duplex ordering has not been verified in the current desktop flow. |
| A19 | 离线运行 | NOT_RUN | Offline current-candidate operation has not been verified with a real physical network disconnect and restore. |
| A20 | 资源协调 | PASS | 已记录实际执行证据。 |
| A21 | STT 性能 | PASS | 已记录实际执行证据。 |
| A22 | Sidecar 性能 | PASS | 已记录实际执行证据。 |
| A23 | 30 分钟耐久 | NOT_RUN | A real operator-involved 30-minute combined endurance run has not been completed on the current candidate. |
| A24 | 隐私 | NOT_RUN | Desktop privacy and diagnostic-package acceptance has not been completed on the current candidate. |
| A25 | 专业任务回归 | PASS | 已记录实际执行证据。 |
| A26 | Ollama 回归 | PASS | 已记录实际执行证据。 |
| A27 | NSIS | PASS | 已记录实际执行证据。 |
| A28 | MSI | BLOCKED | The MSI administrator-install gate requires a real administrator environment and explicit interactive permission confirmation; it remains BLOCKED until actually executed. |

## 发布门禁处理

A01, A02, A03, A07-A11, A20-A22, and A25-A27 are backed by fresh 653f61e execution evidence. A08 is independently reviewed with explicit accuracy limitations. A26 waits only for a test-owned Ollama runner to quiesce and still owner-cleans up if inspection fails.

## TTS 正式化

- `A02` `PASS`：已记录实际执行证据。

## 麦克风与录音

- `A04` `NOT_RUN`：Physical microphone permission denial and grant have not been executed on the current candidate.
- `A05` `NOT_RUN`：Physical device enumeration, switching, disconnect, and busy-device flows have not been executed on the current candidate.
- `A06` `NOT_RUN`：Physical recording start, stop, cancel, and format validation have not been executed on the current candidate.

## STT Provider

- `A08` `PASS`：已记录实际执行证据。

## STT 模型

- `A09` `PASS`：已记录实际执行证据。

## STT API

- `A10` `PASS`：已记录实际执行证据。
- `A11` `PASS`：已记录实际执行证据。

## Voice Session

- `A07` `PASS`：已记录实际执行证据。
- `A12` `NOT_RUN`：Desktop Voice Events ordering has not been verified in the current desktop flow.

## 前端交互

- `A13` `NOT_RUN`：Transcript edit and cancellation have not been verified in the current desktop flow.

## Agent 联动

- `A14` `NOT_RUN`：Manual and automatic send routing have not been verified in the current desktop flow.
- `A15` `NOT_RUN`：Dangerous voice-task confirmation and rejection have not been verified in the current desktop flow.
- `A19` `NOT_RUN`：Offline current-candidate operation has not been verified with a real physical network disconnect and restore.

## 停止与打断

- `A16` `NOT_RUN`：Recording-triggered TTS interruption has not been verified in the current desktop flow.
- `A17` `NOT_RUN`：Four-stage global stop and idempotency have not been verified in the current desktop flow.
- `A18` `NOT_RUN`：Half-duplex ordering has not been verified in the current desktop flow.

## 资源

- `A20` `PASS`：已记录实际执行证据。

## 隐私

- `A24` `NOT_RUN`：Desktop privacy and diagnostic-package acceptance has not been completed on the current candidate.

## 性能

- `A21` `PASS`：已记录实际执行证据。
- `A22` `PASS`：已记录实际执行证据。
- `A25` `PASS`：已记录实际执行证据。

## 30 分钟耐久

- `A23` `NOT_RUN`：A real operator-involved 30-minute combined endurance run has not been completed on the current candidate.

## 故障注入

Every promoted automated case references a schema-5 current-source execution envelope. Live cases reference runner-bound raw evidence; A09 uses a new isolated, confirmed small-model download receipt; the v14 NSIS and MSI packages were rebuilt from the same source identity.

## NSIS

- `A27` `PASS`：已记录实际执行证据。

## MSI

- `A28` `BLOCKED`：The MSI administrator-install gate requires a real administrator environment and explicit interactive permission confirmation; it remains BLOCKED until actually executed.

## 失败、阻塞、跳过和未运行

- `A04` `NOT_RUN`：Physical microphone permission denial and grant have not been executed on the current candidate.
- `A05` `NOT_RUN`：Physical device enumeration, switching, disconnect, and busy-device flows have not been executed on the current candidate.
- `A06` `NOT_RUN`：Physical recording start, stop, cancel, and format validation have not been executed on the current candidate.
- `A12` `NOT_RUN`：Desktop Voice Events ordering has not been verified in the current desktop flow.
- `A13` `NOT_RUN`：Transcript edit and cancellation have not been verified in the current desktop flow.
- `A14` `NOT_RUN`：Manual and automatic send routing have not been verified in the current desktop flow.
- `A15` `NOT_RUN`：Dangerous voice-task confirmation and rejection have not been verified in the current desktop flow.
- `A16` `NOT_RUN`：Recording-triggered TTS interruption has not been verified in the current desktop flow.
- `A17` `NOT_RUN`：Four-stage global stop and idempotency have not been verified in the current desktop flow.
- `A18` `NOT_RUN`：Half-duplex ordering has not been verified in the current desktop flow.
- `A19` `NOT_RUN`：Offline current-candidate operation has not been verified with a real physical network disconnect and restore.
- `A23` `NOT_RUN`：A real operator-involved 30-minute combined endurance run has not been completed on the current candidate.
- `A24` `NOT_RUN`：Desktop privacy and diagnostic-package acceptance has not been completed on the current candidate.
- `A28` `BLOCKED`：The MSI administrator-install gate requires a real administrator environment and explicit interactive permission confirmation; it remains BLOCKED until actually executed.

## 测试统计

- total=28；pass=14；fail=0；blocked=1；skipped=0；not_run=13；not_applicable=0
- pass_rate=1.0；execution_coverage=0.5357

## 已知问题

Complete the remaining physical desktop/microphone, security/privacy, offline, 30-minute endurance, remote-release, and administrator-MSI gates before release approval.

## 技术债务

Physical-device, privacy, endurance, remote-release, and administrator-install validation remain uncollected.

## 后续候选

A04-A06, A12-A19, A23-A24, and A28 require real interactive or administrator conditions.

## 用户决策

MSI administrator-install remains blocked until it is executed in a real administrator environment.

## 机器可读摘要

```json
{
  "version": "14.0.0",
  "release_status": "BLOCKED",
  "git_head": "653f61efadc87c7afa053453be5549402657a151",
  "workspace_clean": true,
  "remote_pushed": false,
  "tests": {
    "total": 28,
    "pass": 14,
    "fail": 0,
    "blocked": 1,
    "skipped": 0,
    "not_run": 13,
    "not_applicable": 0,
    "pass_rate": 1.0,
    "execution_coverage": 0.5357
  },
  "release_gates": {
    "model_download": "PASS",
    "windows_tts": "PASS",
    "melotts": "EXPERIMENTAL_NON_BLOCKING",
    "durability_30m": "NOT_RUN",
    "nsis": "PASS",
    "msi": "BLOCKED"
  },
  "stt": {
    "provider": "",
    "model": "",
    "device": "",
    "compute_type": "",
    "offline": false,
    "microphone_capture": "NOT_RUN",
    "transcription": "NOT_RUN",
    "cancel": "NOT_RUN",
    "model_load": "NOT_RUN",
    "model_unload": "NOT_RUN"
  },
  "voice": {
    "push_to_talk": "NOT_RUN",
    "edit_before_send": "NOT_RUN",
    "auto_send": "NOT_RUN",
    "tts_interrupt": "NOT_RUN",
    "global_stop": "NOT_RUN",
    "voice_session": "NOT_RUN",
    "privacy": "NOT_RUN"
  },
  "performance": {
    "sidecar_median_ms": null,
    "sidecar_p95_ms": null,
    "recording_start_ms": null,
    "stt_model_load_ms": null,
    "stt_5s_audio_ms": null,
    "stt_rtf": null,
    "global_stop_ms": null,
    "not_measured": [
      "sidecar_median_ms",
      "sidecar_p95_ms",
      "recording_start_ms",
      "stt_model_load_ms",
      "stt_5s_audio_ms",
      "stt_rtf",
      "global_stop_ms"
    ]
  },
  "artifacts": {
    "nsis_path": "",
    "msi_path": "",
    "portable_exe_path": "",
    "feedback_path": "build/v1400-evidence/IMPLEMENTATION_FEEDBACK.md",
    "evidence_path": "build/v1400-evidence"
  },
  "evidence_bindings": {}
}
```
