import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "migrate_source_runtime_data.py"
SPEC = importlib.util.spec_from_file_location("migrate_source_runtime_data", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_source_runtime_migration_is_backup_first(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    runtime = tmp_path / "runtime"
    database = repository / "backend" / "data" / "agent.db"
    evaluation = repository / "data" / "evals" / "result.json"
    database.parent.mkdir(parents=True)
    evaluation.parent.mkdir(parents=True)
    database.write_bytes(b"synthetic-database")
    evaluation.write_text('{"fixture": true}\n', encoding="utf-8")

    planned = MODULE.migrate(runtime, apply=False, repository_root=repository)
    assert planned == {"status": "planned", "source_groups": 2, "file_count": 2}
    assert database.exists()

    migrated = MODULE.migrate(runtime, apply=True, repository_root=repository)
    assert migrated["status"] == "migrated"
    assert (runtime / "data" / "agent.db").read_bytes() == b"synthetic-database"
    assert (runtime / "artifacts" / "evals" / "root" / "result.json").is_file()
    assert not database.exists()
    manifest = json.loads(Path(migrated["backup_manifest"]).read_text(encoding="utf-8"))
    assert len(manifest["files"]) == 2
