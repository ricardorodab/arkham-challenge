"""Exercise sealed WAL backups and real authenticated age recovery in disposable paths."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("backup", ROOT / "deploy/backup.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
sys.modules["backup"] = backup
SPEC = importlib.util.spec_from_file_location("backup_restore", ROOT / "deploy/restore.py")
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)


@pytest.fixture
def source(tmp_path):
    data, state, config = (tmp_path / name for name in ("data", "state", "config"))
    release = tmp_path / ("a" * 40)
    for path in (data, state, config, release / "deploy"):
        path.mkdir(parents=True)
    database = sqlite3.connect(state / "web.sqlite3")
    database.execute("PRAGMA journal_mode=WAL")
    database.executescript(
        "CREATE TABLE auth_user(id INTEGER PRIMARY KEY, username TEXT, password TEXT);"
        "CREATE TABLE django_session(session_key TEXT PRIMARY KEY, session_data TEXT);"
        "CREATE TABLE service_savedview(id INTEGER PRIMARY KEY, owner_id INTEGER REFERENCES auth_user(id));"
        "INSERT INTO auth_user VALUES(1,'renamed-admin','unchanged-password-hash');"
        "INSERT INTO auth_user VALUES(2,'analyst','analyst-hash');"
        "INSERT INTO auth_user VALUES(3,'viewer','viewer-hash');"
        "INSERT INTO django_session VALUES('session','signed-data');"
        "INSERT INTO service_savedview VALUES(7,1);"
    )
    database.commit()
    (config / "runtime.env").write_text("OUTAGE_DJANGO_SECRET_KEY=synthetic-secret\n")
    (config / "deployment.env").write_text("OUTAGE_PUBLIC_HOST=fixture.example\n")
    (config / "bootstrap.env").write_text("DO-NOT-BACK-UP-INITIAL-PASSWORDS")
    (config / "worker.env").write_text("EIA_API_KEY=DO-NOT-BACK-UP-EIA-KEY")
    (release / "release.env").write_text("OUTAGE_IMAGE=" + backup.IMAGE_NAMESPACE + "b" * 64 + "\n")
    for name in ("compose.production.yaml", "Caddyfile", "app-control.py", "verify-state.py"):
        shutil.copyfile(ROOT / "deploy" / name, release / "deploy" / name)
    yield data, state, config, release
    database.close()


@pytest.fixture
def age_tools(tmp_path):
    age = os.environ.get("ARKHAM_TEST_AGE") or shutil.which("age")
    keygen = os.environ.get("ARKHAM_TEST_AGE_KEYGEN") or shutil.which("age-keygen")
    if not age or not keygen:
        pytest.skip("Real age CLI is not installed; archive/SQLite contract tests still run.")
    identity = tmp_path / "synthetic-test-identity"
    subprocess.run([keygen, "-o", str(identity)], capture_output=True, check=True, timeout=15)
    recipient = subprocess.run(
        [keygen, "-y", str(identity)], capture_output=True, check=True, timeout=15, text=True
    ).stdout.strip()
    return age, identity, recipient


def capture(source, tmp_path):
    stage = tmp_path / "sealed"
    manifest = backup.capture(*source, stage)
    return stage, manifest


def encrypt(stage, age_tools, target):
    age, _, recipient = age_tools
    with target.open("xb") as output:
        backup.encrypted_stream(
            stage, recipient, lambda incoming: shutil.copyfileobj(incoming, output), age=age
        )


def test_capture_includes_committed_wal_rows_and_excludes_plaintext_credentials(source, tmp_path):
    assert Path(str(source[1] / "web.sqlite3") + "-wal").stat().st_size > 0
    stage, manifest = capture(source, tmp_path)
    assert manifest["databases"]["web"]["tables"]["auth_user"]["rows"] == 3
    assert manifest["databases"]["web"] == backup.fingerprint(source[1] / "web.sqlite3")
    assert manifest["databases"]["catalog"]["exists"] is False
    assert "config/runtime.env" in manifest["files"]
    assert "config/worker.env" not in manifest["files"]
    assert "config/bootstrap.env" not in manifest["files"]
    assert not any(name.endswith(("-wal", "-shm")) for name in manifest["files"])
    with sqlite3.connect(stage / "state/web.sqlite3") as database:
        assert database.execute(
            "SELECT username,password FROM auth_user WHERE id=1"
        ).fetchone() == ("renamed-admin", "unchanged-password-hash")


def add_catalog(source):
    data = source[0]
    catalog = sqlite3.connect(data / "catalog.sqlite3")
    catalog.executescript(
        "CREATE TABLE snapshots(snapshot_id TEXT,relative_path TEXT,manifest_json TEXT,manifest_sha256 TEXT);"
        "CREATE TABLE model_snapshots(model_id TEXT,relative_path TEXT,manifest_json TEXT,manifest_sha256 TEXT);"
    )
    for prefix, table, identity in (
        ("snapshots", "snapshots", "1" * 32),
        ("models", "model_snapshots", "2" * 32),
    ):
        directory = data / prefix / identity
        directory.mkdir(parents=True)
        (directory / "rows.parquet").write_bytes(b"synthetic-immutable-parquet-bytes")
        field = "routes" if prefix == "snapshots" else "datasets"
        manifest = {
            field: {
                "fixture": {
                    "file": "rows.parquet",
                    "sha256": backup.digest(directory / "rows.parquet"),
                }
            }
        }
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        (directory / "manifest.json").write_text(raw)
        catalog.execute(
            f"INSERT INTO {table} VALUES (?,?,?,?)",
            (identity, f"{prefix}/{identity}", raw, backup.digest(directory / "manifest.json")),
        )
    catalog.commit()
    catalog.close()
    return data / "snapshots" / ("1" * 32)


def test_catalog_registrations_preserve_historical_files_and_copy_mutable_metadata(
    source, tmp_path
):
    snapshot = add_catalog(source)
    (source[0] / ".staging").mkdir()
    (source[0] / ".staging/unfinished").write_text("not published")
    stage, manifest = capture(source, tmp_path)
    assert len(backup.registered_paths(stage / "data/catalog.sqlite3")) == 2
    assert not any(".staging" in name for name in manifest["files"])
    original = (stage / "data/snapshots" / ("1" * 32) / "manifest.json").read_bytes()
    (snapshot / "manifest.json").write_text("changed after quiescence")
    assert (stage / "data/snapshots" / ("1" * 32) / "manifest.json").read_bytes() == original


def test_unsafe_catalog_path_is_rejected(source, tmp_path):
    add_catalog(source)
    with sqlite3.connect(source[0] / "catalog.sqlite3") as database:
        database.execute("UPDATE snapshots SET relative_path='../outside'")
    with pytest.raises(backup.BackupError, match="unsafe publication"):
        capture(source, tmp_path)


def test_real_age_round_trip_preserves_accounts_sessions_views_and_catalog(
    source, tmp_path, age_tools
):
    add_catalog(source)
    stage, manifest = capture(source, tmp_path)
    archive = tmp_path / "backup.age"
    encrypt(stage, age_tools, archive)
    assert b"synthetic-secret" not in archive.read_bytes()
    target = tmp_path / "restored"
    result = restore.restore(archive, age_tools[1], target, tmp_path, age=age_tools[0])
    assert result["accounts"] == 3 and result["catalog"] is True
    assert (target / ".verified").is_file() and not (target / ".incomplete").exists()
    assert backup.fingerprint(target / "state/web.sqlite3") == manifest["databases"]["web"]
    assert restore.verify(target) == result


def test_corrupted_ciphertext_never_becomes_a_verified_restore(source, tmp_path, age_tools):
    stage, _ = capture(source, tmp_path)
    archive = tmp_path / "backup.age"
    encrypt(stage, age_tools, archive)
    raw = bytearray(archive.read_bytes())
    raw[-1] ^= 1
    archive.write_bytes(raw)
    target = tmp_path / "failed-restore"
    with pytest.raises((backup.BackupError, tarfile.ReadError, EOFError)):
        restore.restore(archive, age_tools[1], target, tmp_path, age=age_tools[0])
    assert (target / ".incomplete").is_file()
    assert not (target / ".verified").exists()


def test_changed_immutable_bytes_never_commit_a_remote_backup(
    source, tmp_path, age_tools, monkeypatch
):
    snapshot = add_catalog(source)
    stage, _ = capture(source, tmp_path)
    (snapshot / "rows.parquet").write_bytes(b"changed-after-seal")
    committed = []

    class FakeBlob:
        def __init__(self, *_):
            pass

        def upload_blocks(self, name, incoming):
            incoming.read()
            return {}

        def commit(self, *args):
            committed.append(args)

    monkeypatch.setattr(backup, "Blob", FakeBlob)
    with pytest.raises(backup.BackupError, match="sealed-file"):
        backup.upload(
            stage,
            {"account": "fixture", "container": "backups", "recipient": age_tools[2]},
            age=age_tools[0],
        )
    assert committed == []


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "state/../../escape", "state\\escape", "C:/escape"]
)
def test_restore_rejects_path_traversal(source, tmp_path, name):
    _, manifest = capture(source, tmp_path)
    manifest["files"][name] = {"bytes": 0, "sha256": "a" * 64}
    with pytest.raises(backup.BackupError, match="safe restore"):
        restore.manifest_contract(manifest)


def test_restore_refuses_existing_targets_and_foreign_runtime_images(source, tmp_path):
    with pytest.raises(backup.BackupError, match="new direct child"):
        restore.target_path(source[1], tmp_path)
    _, manifest = capture(source, tmp_path)
    manifest["image"] = "ghcr.io/attacker/other@sha256:" + "a" * 64
    with pytest.raises(backup.BackupError, match="trusted Arkham"):
        restore.manifest_contract(manifest)


def test_freshness_requires_matching_remote_encryption_and_rejects_drill_receipts(
    tmp_path, monkeypatch
):
    config = {"account": "fixture", "container": "backups"}
    receipt = {
        **config,
        "created": int(time.time()),
        "blob": "backups/fixture.tar.gz.age",
        "bytes": 10,
        "sha256": "a" * 64,
        "drill": False,
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))

    class Response(io.BytesIO):
        headers = {
            "Content-Length": "10",
            "x-ms-meta-sha256": "a" * 64,
            "x-ms-server-encrypted": "true",
        }

    monkeypatch.setattr(backup.Blob, "request", lambda *_: Response())
    assert backup.check(config, path, 3600)["backup"] == "fresh"
    receipt["drill"] = True
    path.write_text(json.dumps(receipt))
    with pytest.raises(backup.BackupError, match="stale"):
        backup.check(config, path, 3600)


def test_credential_requests_never_follow_redirects():
    with pytest.raises(backup.BackupError, match="must not redirect"):
        backup.NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://attacker.example"
        )
