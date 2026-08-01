from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from app.database import connect, now_iso, rows
from app.runtime_paths import ensure_runtime_layout, runtime_layout


class AudioCache:
    def __init__(self, root: Path | None = None, max_bytes: int = 256 * 1024 * 1024) -> None:
        layout = ensure_runtime_layout(runtime_layout())
        self.root = (root or layout.cache / "tts").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    @staticmethod
    def key(*, provider: str, model_version: str, voice: str, normalized_text: str, speed: float, sample_rate: int) -> str:
        material = "\0".join((provider, model_version, voice, normalized_text, f"{speed:.3f}", str(sample_rate)))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def lookup(self, cache_id: str) -> dict | None:
        records = rows("SELECT * FROM tts_cache_entries WHERE cache_id=?", (cache_id,))
        if not records:
            return None
        record = records[0]
        path = (self.root / str(record["audio_file"])).resolve()
        if path.parent != self.root or not path.is_file():
            with connect() as db:
                db.execute("DELETE FROM tts_cache_entries WHERE cache_id=?", (cache_id,))
            return None
        with connect() as db:
            db.execute("UPDATE tts_cache_entries SET last_used_at=? WHERE cache_id=?", (now_iso(), cache_id))
        return {**record, "path": path}

    def store(self, cache_id: str, source: Path, *, provider: str, model_version: str, voice: str, duration_ms: int, sample_rate: int) -> Path:
        target = (self.root / f"{cache_id}.wav").resolve()
        if target.parent != self.root:
            raise ValueError("invalid TTS cache target")
        temporary = target.with_suffix(".tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
        stamp = now_iso()
        with connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO tts_cache_entries(cache_id,provider,model_version,voice,audio_file,duration_ms,sample_rate,size_bytes,created_at,last_used_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cache_id, provider, model_version, voice, target.name, duration_ms, sample_rate, target.stat().st_size, stamp, stamp),
            )
        self.prune()
        return target

    def list(self) -> list[dict]:
        records = rows("SELECT cache_id,provider,model_version,voice,duration_ms,sample_rate,size_bytes,created_at,last_used_at FROM tts_cache_entries ORDER BY last_used_at DESC")
        return [record for record in records if (self.root / f"{record['cache_id']}.wav").is_file()]

    def delete(self, cache_id: str) -> bool:
        record = self.lookup(cache_id)
        if not record:
            return False
        Path(record["path"]).unlink(missing_ok=True)
        with connect() as db:
            db.execute("DELETE FROM tts_cache_entries WHERE cache_id=?", (cache_id,))
        return True

    def clear(self) -> int:
        records = self.list()
        for record in records:
            (self.root / f"{record['cache_id']}.wav").unlink(missing_ok=True)
        with connect() as db:
            db.execute("DELETE FROM tts_cache_entries")
        return len(records)

    def prune(self) -> None:
        records = self.list()
        total = sum(int(item["size_bytes"]) for item in records)
        for record in reversed(records):
            if total <= self.max_bytes:
                break
            if self.delete(str(record["cache_id"])):
                total -= int(record["size_bytes"])
