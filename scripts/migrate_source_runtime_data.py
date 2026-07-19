from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(path: Path) -> list[Path]:
    return sorted(candidate for candidate in path.rglob("*") if candidate.is_file()) if path.is_dir() else [path]


def _safe_target(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise RuntimeError("runtime migration target escaped the selected data root")
    return target


def migrate(data_root: Path, *, apply: bool, repository_root: Path = REPOSITORY_ROOT) -> dict[str, object]:
    data_root = data_root.resolve()
    repository_root = repository_root.resolve()
    mappings = (
        (repository_root / "backend" / "data" / "agent.db", "data/agent.db"),
        (repository_root / "backend" / "data" / "agent.db-wal", "data/agent.db-wal"),
        (repository_root / "backend" / "data" / "agent.db-shm", "data/agent.db-shm"),
        (repository_root / "backend" / "data" / "backups", "backups/legacy-backend"),
        (repository_root / "backend" / "data" / "logs", "logs/legacy-backend"),
        (repository_root / "backend" / "data" / "evals", "artifacts/evals/backend"),
        (repository_root / "data" / "logs", "logs/legacy-root"),
        (repository_root / "data" / "dev-logs", "logs/legacy-dev"),
        (repository_root / "data" / "evals", "artifacts/evals/root"),
        (repository_root / ".agent-backups", "workspaces/repository-backups"),
    )
    present = [(source, _safe_target(data_root, relative)) for source, relative in mappings if source.exists()]
    report: dict[str, object] = {
        "status": "planned" if not apply else "migrated",
        "source_groups": len(present),
        "file_count": sum(len(_files(source)) for source, _ in present),
    }
    if not apply or not present:
        return report

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_root = data_root / "backups" / f"source-checkout-{stamp}"
    backup_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "migration": "source-checkout-runtime-data",
        "files": [],
    }
    for source, _ in present:
        relative = source.relative_to(repository_root)
        backup = backup_root / relative
        if source.is_dir():
            shutil.copytree(source, backup)
        else:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup)
        for original in _files(source):
            copied = backup / original.relative_to(source) if source.is_dir() else backup
            source_hash = _sha256(original)
            if _sha256(copied) != source_hash:
                raise RuntimeError("runtime backup checksum mismatch")
            manifest["files"].append(
                {
                    "source": original.relative_to(repository_root).as_posix(),
                    "size": original.stat().st_size,
                    "sha256": source_hash,
                }
            )
    (backup_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )

    for source, target in present:
        if target.exists():
            target = target.parent / f"{target.name}.legacy-{stamp}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))

    for candidate in (repository_root / "backend" / "data", repository_root / "data"):
        if candidate.is_dir() and not any(candidate.iterdir()):
            candidate.rmdir()
    report["backup_manifest"] = str(backup_root / "manifest.json")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Move ignored runtime data out of the source checkout.")
    parser.add_argument("--apply", action="store_true", help="Perform the backup-first migration.")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AureliusWu" / "Agent-Dev",
    )
    args = parser.parse_args()
    print(json.dumps(migrate(args.data_root, apply=args.apply), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
