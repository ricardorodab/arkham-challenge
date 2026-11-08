"""Own local run history and transactional publication of immutable bundles.

SQLite is the sole authority for current raw/model references; directories and
manifests are evidence rather than competing publication pointers. Writers hold
``writer_lock`` across recovery and preparation. These transactions cover the
data catalog only, not filesystem writes or the Django refresh-job database.
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
from typing import TYPE_CHECKING, Any

import duckdb

from outage_explorer.data.domain import ROUTES, SCHEMA_VERSION, canonical_json, identifier

if TYPE_CHECKING:
    from outage_explorer.analytics.modeling import ModelBundle


class StorageError(Exception):
    """Carry a caller-safe operational message and stable error code.

    Constructing the exception does not scrub arbitrary values; callers must avoid
    SQL statements, physical paths, credentials or untrusted source text in messages.
    """

    def __init__(self, message: str, *, code: str = "storage_error") -> None:
        """Retain a public message and machine-readable storage error code."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SnapshotRef:
    """Describe an immutable raw bundle, whether unpublished or catalog-resolved.

    The descriptor does not itself guarantee Parquet integrity; ``Catalog.verify``
    performs file/schema/key/logical checks. Treat its path and manifest as pinned
    server-owned inputs rather than selecting data by directory timestamps.
    """

    id: str
    path: Path
    manifest: dict[str, Any]


def _utc_now() -> str:
    """Return an aware UTC ISO timestamp for catalog state transitions."""
    return datetime.now(UTC).isoformat()


def _safe_id(value: str) -> str:
    """Require a lowercase 32-hex bundle/run identifier before path or SQL use.

    Return the unchanged identifier or raise ``StorageError(invalid_identifier)``;
    no caller-controlled path components are accepted by this contract.
    """
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise StorageError("Invalid snapshot or run identifier.", code="invalid_identifier")
    return value


def _sha256(path: Path) -> str:
    """Hash file bytes in one-MiB chunks without modifying the file."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def writer_lock(root: Path) -> Iterator[None]:
    """Hold a nonblocking OS writer lock for the supplied data root.

    Create the root and persistent lock file if needed. Use Windows byte-range
    locking or POSIX flock; release ownership and close the handle on exit, including
    exceptions. Contention raises ``StorageError(already_running)``. The file may
    outlive its owner, so existence/timestamps are never ownership evidence.
    """
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
    """Manage local raw/model registries, history and coherent publication references.

    Opening creates workspace/schema state and enables WAL, FULL synchronization and
    foreign keys. The context manager closes the connection; it does not acquire the
    writer lock. Callers must serialize mutation/recovery and never promote bundle
    directories without the publication methods.
    """

    def __init__(self, root: Path) -> None:
        """Open/create the data catalog and its expected workspace directories.

        Reject symbolic-link workspace directories, initialize schema/publication rows
        and commit setup. SQLite and filesystem failures propagate. Construction can
        create state even when the intended caller operation is a read.
        """
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshots = self.root / "snapshots"
        for directory in (
            self.root / ".staging",
            self.snapshots,
            self.root / "runs",
            self.root / "models",
            self.root / ".model-staging",
        ):
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
            CREATE TABLE IF NOT EXISTS model_builds (
                model_id TEXT PRIMARY KEY,
                source_snapshot_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','interrupted')),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error_code TEXT,
                error_message TEXT
            );
            CREATE TABLE IF NOT EXISTS model_snapshots (
                model_id TEXT PRIMARY KEY,
                source_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
                relative_path TEXT NOT NULL UNIQUE,
                manifest_json TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_publication (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                current_model_id TEXT REFERENCES model_snapshots(model_id)
            );
            INSERT OR IGNORE INTO model_publication(singleton,current_model_id) VALUES(1,NULL);
        """)
        self._db.commit()

    def __enter__(self) -> Catalog:
        """Return this already-open catalog for context-manager use."""
        return self

    def __exit__(self, *_: Any) -> None:
        """Close the connection on context exit without suppressing exceptions."""
        self.close()

    def close(self) -> None:
        """Close the SQLite connection; do not implicitly publish or recover work."""
        self._db.close()

    def recover_interrupted(self) -> int:
        """Mark running ingestion/model-build records interrupted under the writer lock.

        Return the number of ingestion runs changed, not the number of model builds.
        Never downgrade succeeded records or advance references. The caller must already
        own the OS writer lock so a live writer cannot be mistaken for abandoned work.
        """
        with self._db:
            self._db.execute(
                "UPDATE model_builds SET status='interrupted',finished_at=?,error_code='interrupted',"
                "error_message='The process stopped before publication.' WHERE status='running'",
                (_utc_now(),),
            )
            result = self._db.execute(
                "UPDATE runs SET status='interrupted',finished_at=?,error_code='interrupted',"
                "error_message='The process stopped before publication.' WHERE status='running'",
                (_utc_now(),),
            )
        return result.rowcount

    def start_run(self, run_id: str, mode: str, start: str | None, end: str | None) -> None:
        """Insert a committed running ingestion record before extraction.

        Require a safe run ID. Duplicate IDs or the unique active-run constraint raise
        ``StorageError(already_running)``. Scope strings record requested bounds; this
        method does not validate dates or fetch data.
        """
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
        """Resolve an existing direct raw-snapshot directory for a safe ID.

        Reject missing, symbolic-link or redirected paths with ``corrupt_snapshot``.
        This checks containment/shape rather than the bundle's file content.
        """
        path = self.snapshots / _safe_id(snapshot_id)
        if path.is_symlink() or not path.is_dir() or path.resolve() != path:
            raise StorageError("Snapshot directory is missing or unsafe.", code="corrupt_snapshot")
        return path

    @staticmethod
    def _manifest_file(path: Path) -> tuple[dict[str, Any], str]:
        """Read a regular bounded manifest and return its object plus byte hash.

        Require ``manifest.json`` to be nonsymlink, at most two MiB and a JSON object.
        Normalize supported read/parse failures to safe ``corrupt_snapshot`` errors.
        Catalog binding and dataset semantics are checked by the caller.
        """
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
        """Bind a raw registry row to its safe path and unchanged disk manifest.

        Check the exact relative path, manifest byte hash, canonical JSON and identity
        before returning ``SnapshotRef``. Parquet integrity is deferred to verification;
        a matching manifest alone is not a complete bundle audit.
        """
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
        """Return the current raw descriptor, or None before publication.

        Resolve through the catalog reference and verify manifest binding. Do not infer
        current data from filenames; dataset file checks require ``verify``.
        """
        row = self._db.execute(
            "SELECT s.* FROM publication p JOIN snapshots s "
            "ON s.snapshot_id=p.current_snapshot_id WHERE p.singleton=1"
        ).fetchone()
        return None if row is None else self._ref(row)

    def _verify_files(self, ref: SnapshotRef) -> dict[str, Any]:
        """Verify raw-file structure and fingerprints against the supplied manifest.

        Check exact route/file/schema sets, safe paths, physical hashes, natural-key
        uniqueness/non-nullness, row/date/watermark counts and the ordered stored-row-hash
        digest. Return a verification summary; wrap supported engine/read/shape failures
        as ``corrupt_snapshot``. No files or publication references are changed. This
        checks stored fingerprints, not upstream truth or a fresh EIA extraction.
        """
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

    def publish(
        self,
        run_id: str,
        snapshot_dir: Path,
        manifest: dict[str, Any],
        *,
        model: ModelBundle | None = None,
    ) -> None:
        """Atomically register a raw candidate and its matching optional model.

        Require the writer lock, a ready immutable raw directory and an active run.
        Validate disk/source/model bindings before BEGIN IMMEDIATE, then recheck the
        parent, register bundles, advance references and complete ingestion/model-build
        records in one commit. A legacy raw-only call clears the model reference rather
        than reusing a stale model. The Django refresh-job record is separate.

        Raise safe ``StorageError`` for identity, integrity, stale-parent, active-build or
        transaction failures; rollback transaction failures so prior references survive.
        Files are not renamed/deleted here, and failed candidates can remain unreferenced.
        """
        _safe_id(run_id)
        snapshot_id = _safe_id(manifest.get("snapshot_id"))
        path = self._snapshot_path(snapshot_id)
        if Path(snapshot_dir).absolute() != path or manifest.get("run_id") != run_id:
            raise StorageError("Publication identity does not match.", code="invalid_publication")
        stored_manifest, manifest_hash = self._manifest_file(path)
        if canonical_json(stored_manifest) != canonical_json(manifest):
            raise StorageError("Publication manifest differs from disk.", code="corrupt_snapshot")
        self._verify_files(SnapshotRef(snapshot_id, path, stored_manifest))
        if model is not None:
            self._validate_model_publication(model, SnapshotRef(snapshot_id, path, stored_manifest))
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
            if model is not None:
                self._insert_model(model)
            else:
                # Legacy/raw-only callers must invalidate the model pointer,
                # never make an old model appear current for a new raw snapshot.
                self._db.execute(
                    "UPDATE model_publication SET current_model_id=NULL WHERE singleton=1"
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
        """Finish only a still-running ingestion record with caller-sanitized failure.

        Store a bounded public message, caller-sanitized statistics and terminal status.
        Committed success is never downgraded. This method writes catalog state and may
        itself fail if storage is unavailable; callers decide best-effort reporting.
        """
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
        """Return newest-first ingestion records with decoded statistics.

        Order deterministically by timestamp then ID. Require an integer limit 1..1000
        excluding booleans; invalid limits raise ``StorageError(invalid_arguments)``.
        """
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
        """Verify a registered raw snapshot, defaulting to the current publication.

        Return the full raw verification summary. Unknown/absent publication raises
        ``no_snapshot``; malformed IDs or corrupt registry/files raise ``StorageError``.
        No historical directory can be selected without a registry entry.
        """
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

    def snapshot(self, snapshot_id: str) -> SnapshotRef:
        """Resolve a registered historical raw descriptor without full file scans.

        Validate ID and manifest binding; raise ``no_snapshot`` when unregistered. Use
        ``verify`` separately when checksums/schema/keys must also be checked.
        """
        row = self._db.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (_safe_id(snapshot_id),)
        ).fetchone()
        if row is None:
            raise StorageError("No published snapshot was found.", code="no_snapshot")
        return self._ref(row)

    def _model_ref(self, row: sqlite3.Row) -> ModelBundle:
        """Bind a model registry row to an existing safe directory and disk manifest.

        Check exact relative path, physical manifest hash, canonical JSON, model ID and
        registered source ID. Return ``ModelBundle`` without scanning dataset files;
        full contract/content validation belongs to ``verify_model``.
        """
        from outage_explorer.analytics.modeling import ModelBundle

        model_id = _safe_id(row["model_id"])
        path = self.root / "models" / model_id
        if (
            row["relative_path"] != f"models/{model_id}"
            or path.is_symlink()
            or not path.is_dir()
            or path.resolve() != path
        ):
            raise StorageError("Model directory is missing or unsafe.", code="corrupt_model")
        manifest, checksum = self._manifest_file(path)
        if (
            checksum != row["manifest_sha256"]
            or canonical_json(manifest) != row["manifest_json"]
            or manifest.get("model_id") != model_id
            or manifest.get("source_snapshot_id") != row["source_snapshot_id"]
        ):
            raise StorageError("Model manifest integrity check failed.", code="corrupt_model")
        return ModelBundle(model_id, path, manifest)

    def current_model(self) -> ModelBundle | None:
        """Resolve raw/model references together and require matching source identity.

        Return None when no model is published. A coupled SQL read prevents mixing
        reference generations; a source mismatch raises ``stale_model``. The returned
        bundle's manifest is checked, but dataset files are not fully verified here.
        """
        # Both publication pointers are resolved in one read transaction.
        row = self._db.execute(
            "SELECT m.*,p.current_snapshot_id AS active_source "
            "FROM model_publication mp JOIN model_snapshots m ON m.model_id=mp.current_model_id "
            "CROSS JOIN publication p WHERE mp.singleton=1 AND p.singleton=1"
        ).fetchone()
        if row is None:
            return None
        if row["source_snapshot_id"] != row["active_source"]:
            raise StorageError("No matching model for the current source.", code="stale_model")
        return self._model_ref(row)

    def model(self, model_id: str) -> ModelBundle:
        """Resolve a registered historical model or raise ``no_model``.

        Validate the identifier and manifest binding without requiring it to be current.
        Dataset integrity remains a separate verification step.
        """
        row = self._db.execute(
            "SELECT * FROM model_snapshots WHERE model_id=?", (_safe_id(model_id),)
        ).fetchone()
        if row is None:
            raise StorageError("No published model was found.", code="no_model")
        return self._model_ref(row)

    def start_model_build(self, model_id: str, source_snapshot_id: str) -> None:
        """Commit a running model-build record bound to one safe source ID.

        No model files or publication references are created. Identifier validation and
        SQLite constraint errors propagate before preparation proceeds.
        """
        with self._db:
            self._db.execute(
                "INSERT INTO model_builds(model_id,source_snapshot_id,status,started_at) "
                "VALUES(?,?,'running',?)",
                (_safe_id(model_id), _safe_id(source_snapshot_id), _utc_now()),
            )

    def fail_model_build(self, model_id: str, message: str, *, interrupted: bool = False) -> None:
        """Finish a still-running build as failed or interrupted with bounded text.

        Leave succeeded builds unchanged. The message must already be caller-safe;
        recording status is separate from candidate cleanup or reference publication.
        """
        with self._db:
            self._db.execute(
                "UPDATE model_builds SET status=?,finished_at=?,error_code=?,error_message=? "
                "WHERE model_id=? AND status='running'",
                (
                    "interrupted" if interrupted else "failed",
                    _utc_now(),
                    "interrupted" if interrupted else "invalid_model",
                    message[:2000],
                    _safe_id(model_id),
                ),
            )

    def model_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return deterministic newest-first build history with an integer 1..1000 cap.

        Reject booleans and invalid limits with ``StorageError(invalid_arguments)``.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise StorageError("History limit must be 1..1000.", code="invalid_arguments")
        return [
            dict(row)
            for row in self._db.execute(
                "SELECT * FROM model_builds ORDER BY started_at DESC,model_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]

    def _validate_model_publication(self, model: ModelBundle, source: SnapshotRef) -> None:
        """Validate an immutable model's exact path and binding to the supplied raw bundle.

        Require all source hashes/counts to match, then run full model verification.
        Raise ``StorageError`` for unsafe paths, source mismatch or corrupt content.
        This performs analytical file reads but does not advance catalog references.
        """
        from outage_explorer.analytics.model_store import verify_model

        expected = self.root / "models" / _safe_id(model.id)
        if model.path.absolute() != expected or expected.resolve() != expected:
            raise StorageError("Model publication path is invalid.", code="corrupt_model")
        if model.manifest.get("source_snapshot_id") != source.id:
            raise StorageError("Model is bound to another source.", code="stale_model")
        sources = model.manifest.get("source_files", {})
        if set(sources) != {spec.name for spec in ROUTES}:
            raise StorageError("Model source bindings are incomplete.", code="corrupt_model")
        for spec in ROUTES:
            raw_info = source.manifest["routes"][spec.name]
            if (
                sources[spec.name].get("sha256") != raw_info["sha256"]
                or sources[spec.name].get("rows") != raw_info["rows"]
            ):
                raise StorageError("Model source fingerprints differ.", code="corrupt_model")
        verify_model(model)

    def _insert_model(self, model: ModelBundle) -> None:
        """Register and select a model inside the caller's publication transaction.

        Require a matching running build and unchanged disk manifest, insert the immutable
        registry row, advance the model reference and mark its build succeeded. Do not
        commit here; the caller must roll back all changes if any later step fails.
        """
        active = self._db.execute(
            "SELECT status,source_snapshot_id FROM model_builds WHERE model_id=?", (model.id,)
        ).fetchone()
        if (
            active is None
            or active["status"] != "running"
            or active["source_snapshot_id"] != model.manifest["source_snapshot_id"]
        ):
            raise StorageError(
                "Model publication requires a matching active build.", code="invalid_publication"
            )
        manifest, checksum = self._manifest_file(model.path)
        if canonical_json(manifest) != canonical_json(model.manifest):
            raise StorageError("Model manifest differs from disk.", code="corrupt_model")
        self._db.execute(
            "INSERT INTO model_snapshots VALUES(?,?,?,?,?,?)",
            (
                model.id,
                manifest["source_snapshot_id"],
                f"models/{model.id}",
                canonical_json(manifest),
                checksum,
                manifest["created_at"],
            ),
        )
        self._db.execute(
            "UPDATE model_publication SET current_model_id=? WHERE singleton=1", (model.id,)
        )
        self._db.execute(
            "UPDATE model_builds SET status='succeeded',finished_at=? WHERE model_id=? AND status='running'",
            (_utc_now(), model.id),
        )

    def publish_model(self, model: ModelBundle) -> None:
        """Publish a prepared standalone model only while its pinned raw source is current.

        Validate source/model integrity, recheck source identity inside BEGIN IMMEDIATE,
        insert the model and commit. Roll back stale/invalid/SQLite failures and raise a
        safe ``StorageError``. Require external writer-lock ownership; raw publication
        and the separate application refresh-job status are unchanged.
        """
        source = self.snapshot(model.manifest["source_snapshot_id"])
        self._validate_model_publication(model, source)
        try:
            self._db.execute("BEGIN IMMEDIATE")
            current = self._db.execute(
                "SELECT current_snapshot_id FROM publication WHERE singleton=1"
            ).fetchone()[0]
            if current != source.id:
                raise StorageError("The source is no longer current.", code="stale_model")
            self._insert_model(model)
            self._db.commit()
        except StorageError:
            self._db.rollback()
            raise
        except sqlite3.Error:
            self._db.rollback()
            raise StorageError(
                "Model publication transaction failed.", code="publication_failed"
            ) from None
