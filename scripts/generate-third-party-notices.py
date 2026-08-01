from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]


def _license_files(distribution: importlib.metadata.Distribution) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in distribution.files or ():
        normalized = PurePosixPath(str(entry).replace("\\", "/"))
        lowered = normalized.name.casefold()
        if "licenses" not in {part.casefold() for part in normalized.parts} and not (
            lowered.startswith("license") or lowered.startswith("copying") or lowered.startswith("notice")
        ):
            continue
        path = Path(distribution.locate_file(entry))
        if not path.is_file():
            continue
        try:
            result[str(normalized)] = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
    return result


def generate(policy_path: Path) -> str:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    sections = [
        "司忆 / Agent third-party notices",
        "Generated from the exact installed release environment.",
    ]
    for name, expected_version in sorted(policy["packages"].items()):
        distribution = importlib.metadata.distribution(name)
        if distribution.version != expected_version:
            raise RuntimeError(
                f"License inventory version mismatch for {name}: expected {expected_version}, found {distribution.version}"
            )
        files = _license_files(distribution)
        required = policy.get("required_license_payloads", {}).get(name, [])
        for suffix in required:
            if not any(path.replace("\\", "/").endswith(suffix) for path in files):
                raise RuntimeError(f"Required license payload is missing for {name}: {suffix}")
        declared = distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "Not declared"
        sections.extend(["", "=" * 78, f"{name} {distribution.version}", f"Declared license: {declared}"])
        if not files:
            sections.append("No standalone license file was installed; see the declared license above.")
        for filename, body in sorted(files.items()):
            sections.extend(["", f"--- {filename} ---", body])
    return "\n".join(sections).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic Python third-party notices")
    parser.add_argument(
        "--policy",
        type=Path,
        default=ROOT / "packaging" / "python-license-policy.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "release" / "THIRD_PARTY_NOTICES.txt",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(generate(args.policy.resolve()), encoding="utf-8")
    print(json.dumps({"status": "passed", "output": str(output)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
