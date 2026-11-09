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
from types import SimpleNamespace

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
    (config / "deployment.json").write_text(json.dumps({"public_host": "fixture.example"}))
    (config / "key-vault.json").write_text(
        json.dumps({"vault_url": "https://arkhamkv.vault.azure.net", "bootstrap_required": False})
    )
    secrets = tmp_path / "secrets"
    (secrets / "app").mkdir(parents=True)
    (secrets / "app/django-secret-key").write_text("synthetic-secret")
    (secrets / "worker").mkdir()
    (secrets / "worker/eia-api-key").write_text("DO-NOT-BACK-UP-EIA-KEY")
    (secrets / "init").mkdir()
    (secrets / "init/admin-password").write_text("DO-NOT-BACK-UP-INITIAL-PASSWORDS")
    # Old files may remain during a migration. New captures must not consume them.
    (config / "runtime.env").write_text("DO-NOT-READ-LEGACY-ENVIRONMENT")
    (config / "bootstrap.env").write_text("DO-NOT-BACK-UP-INITIAL-PASSWORDS")
    (config / "worker.env").write_text("EIA_API_KEY=DO-NOT-BACK-UP-EIA-KEY")
    (release / "release.json").write_text(json.dumps({"image": backup.IMAGE_NAMESPACE + "b" * 64}))
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
    manifest = backup.capture(*source, stage, secrets=source[2].parent / "secrets")
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
    assert manifest["format"] == 2
    assert (stage / "config/django-secret-key").read_text() == "synthetic-secret"
    assert "config/key-vault.json" in manifest["files"]
    assert "config/runtime.env" not in manifest["files"]
    assert "config/worker.env" not in manifest["files"]
    assert "config/bootstrap.env" not in manifest["files"]
    assert not any(name.endswith(("-wal", "-shm")) for name in manifest["files"])
    assert not any(name.endswith(".env") for name in manifest["files"])
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
    assert (target / "config/django-secret-key").read_text() == "synthetic-secret"


def test_historical_format_one_archive_can_still_be_verified_and_restored(
    source, tmp_path, age_tools
):
    stage, manifest = capture(source, tmp_path)
    # Historical archives retain their original contract. They are never a runtime
    # fallback for new captures or the current isolated secret-file drill.
    for name in list(manifest["files"]):
        if name.startswith("config/") or name == "release/release.json":
            del manifest["files"][name]
    legacy = stage / "config/runtime.env"
    legacy.write_text("OUTAGE_DJANGO_SECRET_KEY=historical-synthetic-key\n")
    manifest["files"]["config/runtime.env"] = {
        "bytes": legacy.stat().st_size,
        "sha256": backup.digest(legacy),
    }
    manifest["format"] = 1
    (stage / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    archive = tmp_path / "historical.age"
    encrypt(stage, age_tools, archive)
    result = restore.restore(
        archive, age_tools[1], tmp_path / "historical-restored", tmp_path, age=age_tools[0]
    )
    assert result["accounts"] == 3


def test_new_manifest_rejects_environment_file_payloads(source, tmp_path):
    _, manifest = capture(source, tmp_path)
    manifest["files"]["config/runtime.env"] = {"bytes": 0, "sha256": "a" * 64}
    with pytest.raises(backup.BackupError, match="does not contain environment"):
        restore.manifest_contract(manifest)


def test_capture_requires_nonempty_runtime_signing_key(source, tmp_path):
    (source[2].parent / "secrets/app/django-secret-key").write_bytes(b"")
    with pytest.raises(backup.BackupError, match="signing key has an invalid size"):
        capture(source, tmp_path)


def test_restored_release_metadata_must_match_sealed_runtime_image(source, tmp_path):
    stage, manifest = capture(source, tmp_path)
    path = stage / "release/release.json"
    path.write_text(json.dumps({"image": backup.IMAGE_NAMESPACE + "c" * 64}))
    manifest["files"]["release/release.json"] = {
        "bytes": path.stat().st_size,
        "sha256": backup.digest(path),
    }
    (stage / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    with pytest.raises(backup.BackupError, match="release metadata differs"):
        restore.verify(stage)


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


def disk_free(monkeypatch, free):
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: SimpleNamespace(free=free))


def test_mutable_copy_budget_rejected_before_destination_creation(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "copy"
    source.write_bytes(b"12345678")
    disk_free(monkeypatch, backup.FREE_RESERVE + backup.IO_OVERHEAD + 7)
    with pytest.raises(backup.BackupError, match="two GiB reserve"):
        backup.copy_with_reserve(source, destination)
    assert not destination.exists()


def test_sqlite_budget_rejected_before_destination_creation(source, tmp_path, monkeypatch):
    live = source[1] / "web.sqlite3"
    with sqlite3.connect(live) as database:
        required = (
            database.execute("PRAGMA page_size").fetchone()[0]
            * database.execute("PRAGMA page_count").fetchone()[0]
            + 65536
        )
    disk_free(monkeypatch, backup.FREE_RESERVE + backup.IO_OVERHEAD + required - 1)
    destination = tmp_path / "snapshot.sqlite3"
    with pytest.raises(backup.BackupError, match="two GiB reserve"):
        backup.sqlite_snapshot(live, destination)
    assert not destination.exists()


def fake_download(monkeypatch, content):
    calls = []

    class Response(io.BytesIO):
        headers = {
            "Content-Length": str(len(content)),
            "x-ms-meta-sha256": backup.hashlib.sha256(content).hexdigest(),
        }

    def request(_, method, name):
        calls.append(method)
        return Response(content if method == "GET" else b"")

    monkeypatch.setattr(backup.Blob, "request", request)
    return calls


def test_download_budget_rejected_before_get_or_output_creation(tmp_path, monkeypatch):
    calls = fake_download(monkeypatch, b"12345678")
    disk_free(monkeypatch, backup.FREE_RESERVE + backup.IO_OVERHEAD + 7)
    destination = tmp_path / "download.age"
    with pytest.raises(backup.BackupError, match="two GiB reserve"):
        restore.download(
            {"account": "fixture", "container": "backups"},
            "backups/fixture.tar.gz.age",
            destination,
        )
    assert calls == ["HEAD"]
    assert not destination.exists()


def test_download_stops_when_concurrent_writes_consume_reserve(tmp_path, monkeypatch):
    fake_download(monkeypatch, b"12345678")
    monkeypatch.setattr(backup, "CHUNK", 4)
    budgets = iter(
        (
            backup.FREE_RESERVE + backup.IO_OVERHEAD + 8,
            backup.FREE_RESERVE + backup.IO_OVERHEAD + 8,
            backup.FREE_RESERVE - 1,
        )
    )
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _: SimpleNamespace(free=next(budgets)))
    destination = tmp_path / "partial.age"
    with pytest.raises(backup.BackupError, match="two GiB reserve"):
        restore.download(
            {"account": "fixture", "container": "backups"},
            "backups/fixture.tar.gz.age",
            destination,
        )
    assert destination.read_bytes() == b"1234"


def test_restore_expanded_budget_rejected_before_payload_writes(
    source, tmp_path, age_tools, monkeypatch
):
    stage, manifest = capture(source, tmp_path)
    archive = tmp_path / "backup.age"
    encrypt(stage, age_tools, archive)
    required = (
        sum(info["bytes"] for info in manifest["files"].values())
        + (stage / "manifest.json").stat().st_size
    )
    disk_free(monkeypatch, backup.FREE_RESERVE + backup.IO_OVERHEAD + required - 1)
    target = tmp_path / "too-large-expanded"
    with pytest.raises(backup.BackupError, match="two GiB reserve"):
        restore.restore(archive, age_tools[1], target, tmp_path, age=age_tools[0])
    assert not (target / "manifest.json").exists()
    assert not (target / "state").exists()
    assert not (target / ".verified").exists()


def test_restore_stops_midstream_when_concurrent_writes_consume_reserve(
    source, tmp_path, age_tools, monkeypatch
):
    stage, _ = capture(source, tmp_path)
    archive = tmp_path / "backup.age"
    encrypt(stage, age_tools, archive)
    target = tmp_path / "disk-shrank"
    partial = target / "config/deployment.json"
    checked = 0

    def capacity(_):
        nonlocal checked
        if partial.exists():
            checked += 1
        return SimpleNamespace(
            free=backup.FREE_RESERVE - 1 if checked >= 2 else 10 * backup.FREE_RESERVE
        )

    monkeypatch.setattr(backup, "CHUNK", 8)
    monkeypatch.setattr(backup.shutil, "disk_usage", capacity)
    with pytest.raises(backup.BackupError, match="two GiB reserve"):
        restore.restore(archive, age_tools[1], target, tmp_path, age=age_tools[0])
    assert partial.stat().st_size == 8
    assert (target / ".incomplete").exists()
    assert not (target / ".verified").exists()


def test_low_disk_after_encryption_never_commits_remote_backup(
    source, tmp_path, age_tools, monkeypatch
):
    stage, _ = capture(source, tmp_path)
    commits = []

    class FakeBlob:
        def __init__(self, *_):
            pass

        def upload_blocks(self, name, incoming):
            incoming.read()
            disk_free(monkeypatch, backup.FREE_RESERVE - 1)
            return {}

        def commit(self, *args):
            commits.append(args)

    monkeypatch.setattr(backup, "Blob", FakeBlob)
    with pytest.raises(backup.BackupError, match="two GiB reserve"):
        backup.upload(
            stage,
            {"account": "fixture", "container": "backups", "recipient": age_tools[2]},
            age=age_tools[0],
        )
    assert commits == []
