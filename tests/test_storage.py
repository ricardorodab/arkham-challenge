"""Publication tests exercise real Parquet, SQLite transactions, and OS locks."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

import duckdb
import pytest

from outage_explorer.data.domain import ROUTES, canonical_json, identifier, validate_row
from outage_explorer.data.storage import Catalog, StorageError, writer_lock


def make_snapshot(catalog, run_id, *, parent=None, duplicate=False):
    path = catalog.root / "snapshots" / run_id
    path.mkdir()
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "snapshot_id": run_id,
        "parent_snapshot_id": parent,
        "created_at": "2026-10-31T12:00:00+00:00",
        "routes": {},
        "http": {"requests": 6},
    }
    with duckdb.connect(config={"threads": 1, "memory_limit": "128MB"}) as connection:
        for spec in ROUTES:
            connection.execute("DROP TABLE IF EXISTS source_data")
            fields = ",".join(f"{identifier(name)} {kind}" for name, kind in spec.columns)
            connection.execute(f"CREATE TABLE source_data({fields})")
            values = []
            for day in (1, 1 if duplicate else 2):
                row = {
                    "period": f"2026-09-{day:02d}",
                    "capacity": "100.000000001",
                    "outage": "1.25",
                    "percentOutage": "1.25",
                    "capacity-units": "megawatts",
                    "outage-units": "megawatts",
                    "percentOutage-units": "percent",
                }
                if "facility" in spec.key_fields:
                    row.update({"facility": "0046", "facilityName": "Test Facility"})
                if "generator" in spec.key_fields:
                    row["generator"] = "01"
                values.append(validate_row(spec, row, date(2026, 9, 1), date(2026, 9, 30)).values)
            placeholders = ",".join("?" for _ in spec.columns)
            connection.executemany(f"INSERT INTO source_data VALUES({placeholders})", values)
            source = path / f"{spec.name}.parquet"
            connection.execute("COPY source_data TO ? (FORMAT PARQUET)", [str(source)])
            row_hash_index = [name for name, _ in spec.columns].index("_row_hash")
            logical = hashlib.sha256()
            for value in values:
                logical.update(value[row_hash_index].encode("ascii") + b"\n")
            manifest["routes"][spec.name] = {
                "route": spec.route,
                "file": source.name,
                "rows": len(values),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "logical_sha256": logical.hexdigest(),
                "schema": [list(column) for column in spec.columns],
                "min_period": "2026-09-01",
                "max_period": "2026-09-01" if duplicate else "2026-09-02",
                "watermark": "2026-09-02",
                "quality": {},
                "extraction_start": "2026-09-01",
                "extraction_end": "2026-09-30",
                "extracted_rows": len(values),
                "added": len(values),
                "changed": 0,
                "deleted": 0,
                "extra_fields": [],
            }
    write_manifest(path, manifest)
    return path, manifest


def write_manifest(path, manifest):
    (path / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")


def begin(catalog):
    run_id = uuid.uuid4().hex
    catalog.start_run(run_id, "full", "2026-09-01", "2026-09-30")
    return run_id


def test_empty_catalog_and_persistent_pragmas(tmp_path):
    with Catalog(tmp_path) as catalog:
        assert catalog.current() is None
        assert catalog.history() == []
        assert catalog._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert catalog._db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert catalog._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(StorageError) as failure:
            catalog.verify()
        assert failure.value.code == "no_snapshot"


def test_publish_reopen_and_stream_verify_all_routes(tmp_path):
    with Catalog(tmp_path) as catalog:
        run_id = begin(catalog)
        path, manifest = make_snapshot(catalog, run_id)
        catalog.publish(run_id, path, manifest)
        assert catalog.current().id == run_id
        assert catalog.history()[0]["status"] == "succeeded"
        assert catalog.history()[0]["statistics"]["routes"]["national"]["rows"] == 2
    with Catalog(tmp_path) as catalog:
        checked = catalog.verify()
        assert checked["verified"] is True
        assert set(checked["routes"]) == {"national", "facility", "generator"}
        assert all(route["rows"] == 2 for route in checked["routes"].values())


def test_late_error_cannot_downgrade_committed_success(tmp_path):
    with Catalog(tmp_path) as catalog:
        run_id = begin(catalog)
        path, manifest = make_snapshot(catalog, run_id)
        catalog.publish(run_id, path, manifest)
        catalog.fail_run(run_id, "late_error", "A diagnostic could not be written.", {})
        assert catalog.history()[0]["status"] == "succeeded"
        assert catalog.current().id == run_id


def test_missing_last_route_preserves_previous_publication(tmp_path):
    with Catalog(tmp_path) as catalog:
        previous = begin(catalog)
        path, manifest = make_snapshot(catalog, previous)
        catalog.publish(previous, path, manifest)
        latest = begin(catalog)
        path, manifest = make_snapshot(catalog, latest, parent=previous)
        (path / "generator.parquet").unlink()
        with pytest.raises(StorageError) as failure:
            catalog.publish(latest, path, manifest)
        assert failure.value.code == "corrupt_snapshot"
        catalog.fail_run(latest, "invalid_data", "Generator file was not completed.", {"pages": 2})
        assert catalog.current().id == previous
        assert catalog.history()[0]["status"] == "failed"
        assert catalog.history()[0]["statistics"] == {"pages": 2}


def test_transaction_failure_rolls_back_pointer_snapshot_and_success(tmp_path):
    with Catalog(tmp_path) as catalog:
        previous = begin(catalog)
        path, manifest = make_snapshot(catalog, previous)
        catalog.publish(previous, path, manifest)
        latest = begin(catalog)
        path, manifest = make_snapshot(catalog, latest, parent=previous)
        catalog._db.execute("""
            CREATE TRIGGER simulate_crash BEFORE UPDATE ON publication
            BEGIN SELECT RAISE(ABORT,'simulated commit boundary failure'); END;
        """)
        with pytest.raises(StorageError) as failure:
            catalog.publish(latest, path, manifest)
        assert failure.value.code == "publication_failed"
        assert catalog.current().id == previous
        assert catalog.history()[0]["status"] == "running"
        with pytest.raises(StorageError) as missing:
            catalog.verify(latest)
        assert missing.value.code == "no_snapshot"
        assert path.is_dir(), "Unpublished evidence is retained, not deleted."


@pytest.mark.parametrize(
    "field,value",
    [
        ("rows", 999),
        ("rows", True),
        ("rows", 2.0),
        ("sha256", "0" * 64),
        ("logical_sha256", "0" * 64),
        ("file", "../outside.parquet"),
        ("schema", [["period", "VARCHAR"]]),
        ("min_period", "2007-01-01"),
        ("watermark", "2099-12-31"),
    ],
)
def test_manifest_contract_errors_never_publish(tmp_path, field, value):
    with Catalog(tmp_path) as catalog:
        run_id = begin(catalog)
        path, manifest = make_snapshot(catalog, run_id)
        manifest["routes"]["national"][field] = value
        write_manifest(path, manifest)
        with pytest.raises(StorageError) as failure:
            catalog.publish(run_id, path, manifest)
        assert failure.value.code == "corrupt_snapshot"
        assert catalog.current() is None


def test_duplicate_keys_never_publish(tmp_path):
    with Catalog(tmp_path) as catalog:
        run_id = begin(catalog)
        path, manifest = make_snapshot(catalog, run_id, duplicate=True)
        with pytest.raises(StorageError) as failure:
            catalog.publish(run_id, path, manifest)
        assert failure.value.code == "corrupt_snapshot"


def test_parquet_tampering_is_detected(tmp_path):
    with Catalog(tmp_path) as catalog:
        run_id = begin(catalog)
        path, manifest = make_snapshot(catalog, run_id)
        catalog.publish(run_id, path, manifest)
        with (path / "facility.parquet").open("ab") as stream:
            stream.write(b"unexpected bytes")
        with pytest.raises(StorageError) as failure:
            catalog.verify()
        assert failure.value.code == "corrupt_snapshot"


def test_manifest_tampering_is_detected_before_serving_current(tmp_path):
    with Catalog(tmp_path) as catalog:
        run_id = begin(catalog)
        path, manifest = make_snapshot(catalog, run_id)
        catalog.publish(run_id, path, manifest)
        manifest["routes"]["national"]["quality"] = {"tampered": True}
        write_manifest(path, manifest)
        with pytest.raises(StorageError) as failure:
            catalog.current()
        assert failure.value.code == "corrupt_snapshot"


def test_recovery_marks_uncommitted_run_without_deleting_evidence(tmp_path):
    with writer_lock(tmp_path), Catalog(tmp_path) as catalog:
        run_id = begin(catalog)
        staging = tmp_path / ".staging" / run_id
        staging.mkdir()
        (staging / "evidence.txt").write_text("partial extraction", encoding="utf-8")
    with writer_lock(tmp_path), Catalog(tmp_path) as catalog:
        assert catalog.recover_interrupted() == 1
        assert catalog.recover_interrupted() == 0
        assert catalog.history()[0]["status"] == "interrupted"
        assert staging.is_dir()
        assert catalog.current() is None


def test_stale_parent_cannot_replace_a_newer_snapshot(tmp_path):
    with Catalog(tmp_path) as catalog:
        previous = begin(catalog)
        path, manifest = make_snapshot(catalog, previous)
        catalog.publish(previous, path, manifest)
        latest = begin(catalog)
        path, manifest = make_snapshot(catalog, latest, parent=None)
        with pytest.raises(StorageError) as failure:
            catalog.publish(latest, path, manifest)
        assert failure.value.code == "stale_snapshot"
        assert catalog.current().id == previous


def test_only_one_run_can_be_active_even_without_caller_lock(tmp_path):
    with Catalog(tmp_path) as catalog:
        begin(catalog)
        with pytest.raises(StorageError) as failure:
            begin(catalog)
        assert failure.value.code == "already_running"


def test_identifiers_cannot_escape_storage_root(tmp_path):
    with Catalog(tmp_path) as catalog:
        for identifier_value in ("../secret", "../../key.txt", "not-a-uuid", "", None):
            with pytest.raises(StorageError) as failure:
                catalog.verify(identifier_value if identifier_value is not None else "../")
            assert failure.value.code == "invalid_identifier"


def test_symbolic_link_storage_directory_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "data"
    root.mkdir()
    try:
        (root / "snapshots").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires privileges unavailable on this host.")
    with pytest.raises(StorageError):
        Catalog(root)


def test_writer_lock_excludes_another_process_and_releases(tmp_path):
    script = """
import sys
from pathlib import Path
from outage_explorer.data.storage import StorageError, writer_lock
try:
    with writer_lock(Path(sys.argv[1])):
        print('acquired')
except StorageError as error:
    print(error.code)
    raise SystemExit(73)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    arguments = [sys.executable, "-c", script, str(tmp_path)]
    with writer_lock(tmp_path):
        attempt = subprocess.run(
            arguments, capture_output=True, text=True, timeout=15, env=environment, check=False
        )
        assert attempt.returncode == 73, attempt.stderr
        assert attempt.stdout.strip() == "already_running"
    assert (tmp_path / ".writer.lock").exists()
    attempt = subprocess.run(
        arguments, capture_output=True, text=True, timeout=15, env=environment, check=False
    )
    assert attempt.returncode == 0, attempt.stderr
    assert attempt.stdout.strip() == "acquired"


def test_failure_metadata_persists_after_reopen(tmp_path):
    with Catalog(tmp_path) as catalog:
        run_id = begin(catalog)
        catalog.fail_run(
            run_id, "network_error", "The source did not respond.", {"requests": 3, "retries": 2}
        )
    with Catalog(tmp_path) as catalog:
        record = catalog.history()[0]
        assert record["run_id"] == run_id
        assert record["error_code"] == "network_error"
        assert record["statistics"] == {"requests": 3, "retries": 2}
        assert record["finished_at"]


def test_keyboard_interrupt_is_recorded_as_interrupted_not_failed(tmp_path):
    with Catalog(tmp_path) as catalog:
        run_id = begin(catalog)
        catalog.fail_run(
            run_id, "interrupted", "The operator interrupted the run.", {"requests": 1}
        )
        record = catalog.history()[0]
        assert record["status"] == "interrupted"
        assert record["error_code"] == "interrupted"
        assert record["statistics"] == {"requests": 1}
        assert catalog.current() is None
