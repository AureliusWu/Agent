from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CASE_RE = re.compile(
    r"^\|\s*`(?P<id>[A-Z]+-\d{3})`\s*\|\s*(?P<severity>P[01])\s*\|"
    r"\s*(?P<title>[^|]+?)\s*\|\s*(?P<pre>[^|]+?)\s*\|"
    r"\s*(?P<expected>[^|]+?)\s*\|\s*(?P<evidence>[^|]+?)\s*\|$"
)
VALID_STATUSES = {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "NOT_APPLICABLE"}


def load_cases(path: Path) -> list[dict[str, str]]:
    cases: list[dict[str, str]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        match = CASE_RE.match(raw)
        if match:
            cases.append(
                {key: value.strip() for key, value in match.groupdict().items()}
            )
    ids = [case["id"] for case in cases]
    severity = Counter(case["severity"] for case in cases)
    if (
        len(cases) != 220
        or len(set(ids)) != 220
        or severity != {"P0": 175, "P1": 45}
    ):
        raise ValueError(
            f"unexpected test-set shape: total={len(cases)}, "
            f"unique={len(set(ids))}, severity={dict(severity)}"
        )
    return cases


def load_results(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("results file must be a JSON object keyed by case id")
    return payload


def validate_result(case_id: str, result: dict[str, Any]) -> None:
    status = str(result.get("status") or "")
    if status not in VALID_STATUSES:
        raise ValueError(f"{case_id}: invalid status {status!r}")
    evidence = result.get("evidence")
    if status == "PASS":
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f"{case_id}: PASS requires at least one evidence record")
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise ValueError(f"{case_id}: evidence[{index}] must be an object")
            for field in ("command", "artifact", "recorded_at", "build_id"):
                if not str(item.get(field) or "").strip():
                    raise ValueError(
                        f"{case_id}: PASS evidence[{index}] is missing {field}"
                    )
            artifact = Path(str(item["artifact"]))
            if not artifact.is_absolute():
                artifact = ROOT / artifact
            if not artifact.exists():
                raise ValueError(
                    f"{case_id}: PASS evidence artifact does not exist: {artifact}"
                )
            if artifact.suffix.lower() == ".json":
                payload = json.loads(artifact.read_text(encoding="utf-8-sig"))
                if isinstance(payload, dict):
                    artifact_status = str(payload.get("status") or "").lower()
                    if artifact_status in {"failed", "blocked", "error"}:
                        raise ValueError(
                            f"{case_id}: PASS evidence artifact is not passed: "
                            f"{artifact} ({artifact_status})"
                        )
                    if artifact.name == "backend-gate.json":
                        if artifact_status != "passed":
                            raise ValueError(
                                f"{case_id}: backend gate is not passed"
                            )
                        collected = set(payload.get("collected_nodeids") or [])
                        requested = re.findall(
                            r"tests/[^\s]+::[^\s]+", str(item["command"])
                        )
                        missing = sorted(
                            nodeid for nodeid in requested if nodeid not in collected
                        )
                        if missing:
                            raise ValueError(
                                f"{case_id}: backend evidence did not collect "
                                f"requested node(s): {', '.join(missing)}"
                            )
    elif status in {"FAIL", "BLOCKED"} and not str(
        result.get("reason") or ""
    ).strip():
        raise ValueError(f"{case_id}: {status} requires a reason")


def render(
    cases: list[dict[str, str]],
    results: dict[str, dict[str, Any]],
    commit: str,
) -> str:
    unknown = sorted(set(results) - {case["id"] for case in cases})
    if unknown:
        raise ValueError(f"results contain unknown case ids: {', '.join(unknown)}")

    rows: list[dict[str, Any]] = []
    for case in cases:
        result = results.get(case["id"], {"status": "NOT_RUN"})
        validate_result(case["id"], result)
        rows.append({**case, **result})

    status_counts = Counter(row["status"] for row in rows)
    severity_counts = {
        severity: Counter(
            row["status"] for row in rows if row["severity"] == severity
        )
        for severity in ("P0", "P1")
    }
    p0_pass_rate = severity_counts["P0"]["PASS"] / 175
    p1_pass_rate = severity_counts["P1"]["PASS"] / 45
    release_ready = (
        p0_pass_rate == 1.0
        and p1_pass_rate >= 0.95
        and status_counts["FAIL"] == 0
        and status_counts["BLOCKED"] == 0
        and status_counts["NOT_RUN"] == 0
    )

    lines = [
        "# 司忆 v8.0.0 测试矩阵",
        "",
        "- 基线测试集：`司忆_v7.0.0_测试集.md`（发布版本号解释为 v8.0.0）",
        f"- 当前源码提交：`{commit}`",
        "- 判定规则：PASS 必须绑定实际运行证据；代码审查、推断、缺环境或未执行不得判定 PASS。",
        f"- 发布门禁：`{'READY' if release_ready else 'NOT READY'}`",
        "",
        "## 汇总",
        "",
        "| 范围 | PASS | FAIL | BLOCKED | NOT_RUN | NOT_APPLICABLE | 通过率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for severity, total in (("P0", 175), ("P1", 45)):
        counts = severity_counts[severity]
        lines.append(
            f"| {severity} | {counts['PASS']} | {counts['FAIL']} | "
            f"{counts['BLOCKED']} | {counts['NOT_RUN']} | "
            f"{counts['NOT_APPLICABLE']} | {counts['PASS'] / total:.2%} |"
        )
    lines.extend(
        [
            f"| 合计 | {status_counts['PASS']} | {status_counts['FAIL']} | "
            f"{status_counts['BLOCKED']} | {status_counts['NOT_RUN']} | "
            f"{status_counts['NOT_APPLICABLE']} | "
            f"{status_counts['PASS'] / 220:.2%} |",
            "",
            "## 逐项结果",
            "",
            "| ID | 等级 | 用例 | 状态 | 实际证据 / 阻塞原因 |",
            "|---|---|---|---|---|",
        ]
    )
    for row in rows:
        if row["status"] == "PASS":
            descriptions = []
            for item in row["evidence"]:
                descriptions.append(
                    f"`{item['command']}` → `{item['artifact']}` "
                    f"({item['recorded_at']}, build `{item['build_id']}`)"
                )
            detail = "<br>".join(descriptions)
        else:
            detail = str(row.get("reason") or "尚未执行")
        detail = detail.replace("|", "\\|")
        title = row["title"].replace("|", "\\|")
        lines.append(
            f"| {row['id']} | {row['severity']} | {title} | "
            f"{row['status']} | {detail} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the evidence-backed v8.0.0 220-case matrix"
    )
    parser.add_argument("--test-set", type=Path, required=True)
    parser.add_argument("--results", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "8.0.0" / "TEST_MATRIX.md",
    )
    parser.add_argument("--commit", default="working-tree")
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render(
            load_cases(args.test_set.resolve()),
            load_results(args.results),
            args.commit,
        ),
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
