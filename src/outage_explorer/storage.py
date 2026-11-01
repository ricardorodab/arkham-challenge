"""Local, transactional publication of immutable connector snapshots.

The caller holds ``writer_lock`` across recovery and the entire extraction. SQLite
is the sole publication authority; snapshot directories are never edited here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from .domain import ROUTES, SCHEMA_VERSION, canonical_json, identifier


class StorageError(Exception):
    """A safe operational message without database statements or source values."""

    def __init__(self, message: str, *, code: str = "storage_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SnapshotRef:
    id: str
    path: Path
    manifest: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise StorageError("Invalid snapshot or run identifier.", code="invalid_identifier")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def writer_lock(root: Path) -> Iterator[None]:
    """Nonblocking OS lock; the persistent file itself never means 'locked'."""
    handle = None
    acquired = False
    try:
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        handle = (root / ".writer.lock").open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise StorageError(
                "Another connector run holds the writer lock.", code="already_running"
            ) from None
        yield
    finally:
        if handle is not None:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


class Catalog:
    """Durable local run history and one current immutable snapshot."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshots = self.root / "snapshots"
        for directory in (self.root / ".staging", self.snapshots, self.root / "runs"):
            if directory.is_symlink():
                raise StorageError("Storage directories cannot be symbolic links.")
            directory.mkdir(exist_ok=True)
        self._db = sqlite3.connect(self.root / "catalog.sqlite3", timeout=5)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                start_period TEXT,
                end_period TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL CHECK (
                    status IN ('running','succeeded','failed','interrupted')
                ),
                error_code TEXT,
                error_message TEXT,
                statistics_json TEXT NOT NULL DEFAULT '{}',
                snapshot_id TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_running_run
                ON runs ((1)) WHERE status = 'running';
            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL UNIQUE,
                manifest_json TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS publication (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                current_snapshot_id TEXT REFERENCES snapshots(snapshot_id)
            );
            INSERT OR IGNORE INTO publication(singleton,current_snapshot_id) VALUES(1,NULL);
        """)
        self._db.commit()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def recover_interrupted(self) -> int:
        """Call only while holding the OS writer lock, never from a reader."""
        with self._db:
            result = self._db.execute(
                "UPDATE runs SET status='interrupted',finished_at=?,error_code='interrupted',"
                "error_message='The process stopped before publication.' WHERE status='running'",
                (_utc_now(),),
            )
        return result.rowcount

    def start_run(self, run_id: str, mode: str, start: str | None, end: str | None) -> None:
        _safe_id(run_id)
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO runs(run_id,mode,start_period,end_period,started_at,status) "
                    "VALUES(?,?,?,?,?,'running')",
                    (run_id, mode, start, end, _utc_now()),
                )
        except sqlite3.IntegrityError:
            raise StorageError(
                "A run already exists or another run is active.", code="already_running"
            ) from None

    def _snapshot_path(self, snapshot_id: str) -> Path:
        path = self.snapshots / _safe_id(snapshot_id)
        if path.is_symlink() or not path.is_dir() or path.resolve() != path:
            raise StorageError("Snapshot directory is missing or unsafe.", code="corrupt_snapshot")
        return path

    @staticmethod
    def _manifest_file(path: Path) -> tuple[dict[str, Any], str]:
        source = path / "manifest.json"
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 2 * 1024 * 1024:
            raise StorageError("Snapshot manifest is missing or unsafe.", code="corrupt_snapshot")
        try:
            raw = source.read_bytes()
            value = json.loads(raw)
        except (OSError, ValueError, UnicodeError):
            raise StorageError(
                "Snapshot manifest cannot be read.", code="corrupt_snapshot"
            ) from None
        if not isinstance(value, dict):
            raise StorageError("Snapshot manifest must be an object.", code="corrupt_snapshot")
        return value, hashlib.sha256(raw).hexdigest()

    def _ref(self, row: sqlite3.Row) -> SnapshotRef:
        snapshot_id = _safe_id(row["snapshot_id"])
        if row["relative_path"] != f"snapshots/{snapshot_id}":
            raise StorageError("Snapshot catalog path is invalid.", code="corrupt_snapshot")
        path = self._snapshot_path(snapshot_id)
        manifest, digest = self._manifest_file(path)
        if (
            digest != row["manifest_sha256"]
            or canonical_json(manifest) != row["manifest_json"]
            or manifest.get("snapshot_id") != snapshot_id
        ):
            raise StorageError("Snapshot manifest integrity check failed.", code="corrupt_snapshot")
        return SnapshotRef(snapshot_id, path, manifest)

    def current(self) -> SnapshotRef | None:
        row = self._db.execute(
            "SELECT s.* FROM publication p JOIN snapshots s "
            "ON s.snapshot_id=p.current_snapshot_id WHERE p.singleton=1"
        ).fetchone()
        return None if row is None else self._ref(row)

    def _verify_files(self, ref: SnapshotRef) -> dict[str, Any]:
        manifest = ref.manifest
        if (
            manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("snapshot_id") != ref.id
            or not isinstance(manifest.get("routes"), dict)
            or set(manifest["routes"]) != {spec.name for spec in ROUTES}
        ):
            raise StorageError("Snapshot manifest contract is invalid.", code="corrupt_snapshot")
        verified: dict[str, Any] = {}
        connection = duckdb.connect(config={"threads": 1, "memory_limit": "256MB"})
        try:
            for spec in ROUTES:
                info = manifest["routes"][spec.name]
                if not isinstance(info, dict) or info.get("file") != f"{spec.name}.parquet":
                    raise StorageError(
                        "Snapshot data filename is invalid.", code="corrupt_snapshot"
                    )
                source = ref.path / info["file"]
                if (
                    source.is_symlink()
                    or not source.is_file()
                    or source.resolve().parent != ref.path
                ):
                    raise StorageError(
                        "Snapshot data file is missing or unsafe.", code="corrupt_snapshot"
                    )
                if info.get("route") != spec.route or _sha256(source) != info.get("sha256"):
                    raise StorageError(
                        "Snapshot file integrity check failed.", code="corrupt_snapshot"
                    )
                schema = [
                    [row[0], row[1]]
                    for row in connection.execute(
                        "DESCRIBE SELECT * FROM read_parquet(?)", [str(source)]
                    ).fetchall()
                ]
                if schema != [[name, kind] for name, kind in spec.columns] or schema != [
                    list(column) for column in info.get("schema", [])
                ]:
                    raise StorageError("Snapshot Parquet schema differs.", code="corrupt_snapshot")
                keys = ",".join(identifier(key) for key in spec.key_fields)
                nulls = " OR ".join(f"{identifier(key)} IS NULL" for key in spec.key_fields)
                count, null_count, unique_count, minimum, maximum = connection.execute(
                    f"SELECT count(*),count(*) FILTER(WHERE {nulls}),"
                    f"count(DISTINCT ({keys})),min(period),max(period) FROM read_parquet(?)",
                    [str(source)],
                ).fetchone()
                if (
                    not isinstance(info.get("rows"), int)
                    or isinstance(info.get("rows"), bool)
                    or count != info.get("rows")
                    or null_count
                    or count != unique_count
                ):
                    raise StorageError(
                        "Snapshot row count or keys differ.", code="corrupt_snapshot"
                    )
                if (
                    (minimum.isoformat() if minimum else None) != info.get("min_period")
                    or (maximum.isoformat() if maximum else None) != info.get("max_period")
                    or (maximum.isoformat() if maximum else None) != info.get("watermark")
                ):
                    raise StorageError("Snapshot date coverage differs.", code="corrupt_snapshot")
                logical = hashlib.sha256()
                cursor = connection.execute(
                    f'SELECT "_row_hash" FROM read_parquet(?) ORDER BY {keys}', [str(source)]
                )
                while batch := cursor.fetchmany(8192):
                    for (row_hash,) in batch:
                        if (
                            not isinstance(row_hash, str)
                            or re.fullmatch(r"[0-9a-f]{64}", row_hash) is None
                        ):
                            raise StorageError(
                                "Snapshot row fingerprint is invalid.", code="corrupt_snapshot"
                            )
                        logical.update(row_hash.encode("ascii") + b"\n")
                if logical.hexdigest() != info.get("logical_sha256"):
                    raise StorageError(
                        "Snapshot logical fingerprint differs.", code="corrupt_snapshot"
                    )
                verified[spec.name] = {
                    "rows": count,
                    "sha256": info["sha256"],
                    "logical_sha256": logical.hexdigest(),
                }
        except (duckdb.Error, OSError, TypeError, ValueError, KeyError):
            raise StorageError("Snapshot verification failed.", code="corrupt_snapshot") from None
        finally:
            connection.close()
        return {"snapshot_id": ref.id, "verified": True, "routes": verified}

    def publish(self, run_id: str, snapshot_dir: Path, manifest: dict[str, Any]) -> None:
        _safe_id(run_id)
        snapshot_id = _safe_id(manifest.get("snapshot_id"))
        path = self._snapshot_path(snapshot_id)
        if Path(snapshot_dir).absolute() != path or manifest.get("run_id") != run_id:
            raise StorageError("Publication identity does not match.", code="invalid_publication")
        stored_manifest, manifest_hash = self._manifest_file(path)
        if canonical_json(stored_manifest) != canonical_json(manifest):
            raise StorageError("Publication manifest differs from disk.", code="corrupt_snapshot")
        self._verify_files(SnapshotRef(snapshot_id, path, stored_manifest))
        statistics = {
            "snapshot_id": snapshot_id,
            "http": manifest.get("http", {}),
            "routes": manifest["routes"],
        }
        try:
            self._db.execute("BEGIN IMMEDIATE")
            current_id = self._db.execute(
                "SELECT current_snapshot_id FROM publication WHERE singleton=1"
            ).fetchone()[0]
            if manifest.get("parent_snapshot_id") != current_id:
                raise StorageError(
                    "The parent snapshot is no longer current.", code="stale_snapshot"
                )
            active = self._db.execute(
                "SELECT status FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if active is None or active["status"] != "running":
                raise StorageError(
                    "Publication requires an active run.", code="invalid_publication"
                )
            self._db.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?,?,?)",
                (
                    snapshot_id,
                    f"snapshots/{snapshot_id}",
                    canonical_json(stored_manifest),
                    manifest_hash,
                    manifest.get("created_at", _utc_now()),
                    run_id,
                ),
            )
            self._db.execute(
                "UPDATE publication SET current_snapshot_id=? WHERE singleton=1", (snapshot_id,)
            )
            self._db.execute(
                "UPDATE runs SET status='succeeded',finished_at=?,snapshot_id=?,statistics_json=? "
                "WHERE run_id=?",
                (_utc_now(), snapshot_id, canonical_json(statistics), run_id),
            )
            self._db.commit()
        except StorageError:
            self._db.rollback()
            raise
        except sqlite3.Error:
            self._db.rollback()
            raise StorageError(
                "Snapshot publication transaction failed.", code="publication_failed"
            ) from None

    def fail_run(self, run_id: str, code: str, message: str, statistics: dict[str, Any]) -> None:
        """Store only a caller-sanitized public error; never downgrade committed success."""
        with self._db:
            self._db.execute(
                "UPDATE runs SET status=?,finished_at=?,error_code=?,error_message=?,"
                "statistics_json=? WHERE run_id=? AND status='running'",
                (
                    "interrupted" if code == "interrupted" else "failed",
                    _utc_now(),
                    code,
                    message[:2000],
                    canonical_json(statistics),
                    _safe_id(run_id),
                ),
            )

    def history(self, limit: int = 10) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise StorageError(
                "History limit must be between 1 and 1000.", code="invalid_arguments"
            )
        rows = self._db.execute(
            "SELECT * FROM runs ORDER BY started_at DESC,run_id DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for row in rows:
            record = dict(row)
            record["statistics"] = json.loads(record.pop("statistics_json"))
            result.append(record)
        return result

    def verify(self, snapshot_id: str | None = None) -> dict[str, Any]:
        if snapshot_id is None:
            ref = self.current()
        else:
            row = self._db.execute(
                "SELECT * FROM snapshots WHERE snapshot_id=?", (_safe_id(snapshot_id),)
            ).fetchone()
            ref = None if row is None else self._ref(row)
        if ref is None:
            raise StorageError("No published snapshot was found.", code="no_snapshot")
        return self._verify_files(ref)
