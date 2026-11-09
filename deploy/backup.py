"""Capture quiesced Arkham state and stream age-encrypted backups to private Azure Blob.

The host wrapper owns application/writer quiescence. This module never stops services,
changes live databases, loads a recovery private key, or uses a storage account key/SAS.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

CHUNK = 4 * 1024 * 1024
MAX_BYTES = 30 * 1024**3
MAX_FILES = 100_000
MAX_MANIFEST = 16 * 1024 * 1024
FREE_RESERVE = 2 * 1024**3
IO_OVERHEAD = 65536
IMAGE_NAMESPACE = "ghcr.io/ricardorodab/arkham-challenge-vm@sha256:"


class BackupError(RuntimeError):
    """Expose deliberate operational messages without response bodies or secret values."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise BackupError("Credential-bearing requests must not redirect.")


def require_space(path, required=0):
    """Keep two GiB free on the actual destination filesystem, including concurrent use."""
    destination = Path(path).absolute()
    while not destination.exists():
        destination = destination.parent
    # Keep allocation rounding/inodes outside the operational reserve as well.
    if required < 0 or shutil.disk_usage(destination).free < required + FREE_RESERVE + IO_OVERHEAD:
        raise BackupError("Insufficient disk space while preserving the two GiB reserve.")


def copy_with_reserve(source, destination):
    expected = Path(source).stat().st_size
    require_space(destination, expected)
    written = 0
    with Path(source).open("rb") as incoming, Path(destination).open("xb") as output:
        os.chmod(destination, 0o600)
        while block := incoming.read(CHUNK):
            if written + len(block) > expected:
                raise BackupError("A mutable backup input grew during its quiesced copy.")
            require_space(destination, expected - written)
            output.write(block)
            written += len(block)
    if written != expected:
        raise BackupError("A mutable backup input changed size during its quiesced copy.")


def write_with_reserve(path, value):
    require_space(path, len(value))
    with Path(path).open("xb") as output:
        os.chmod(path, 0o600)
        for offset in range(0, len(value), CHUNK):
            require_space(path, len(value) - offset)
            output.write(value[offset : offset + CHUNK])
        output.flush()
        os.fsync(output.fileno())


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            result.update(block)
    return result.hexdigest()


def regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.resolve() != path.absolute():
        raise BackupError("A backup input is missing or contains a symbolic link.")
    return path


def fingerprint(path):
    spec = importlib.util.spec_from_file_location(
        "backup_application_control", Path(__file__).with_name("app-control.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.database_state(Path(path))


def sqlite_snapshot(source, destination):
    regular(source)
    require_space(destination)
    with closing(sqlite3.connect(Path(source).as_uri() + "?mode=ro", uri=True)) as live:
        page_size = live.execute("PRAGMA page_size").fetchone()[0]
        pages = live.execute("PRAGMA page_count").fetchone()[0]
        require_space(destination, page_size * pages + 65536)
        with closing(sqlite3.connect(destination)) as saved:
            live.backup(
                saved,
                pages=64,
                progress=lambda status, remaining, total: require_space(
                    destination, remaining * page_size
                ),
            )
            if saved.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BackupError("A SQLite snapshot failed its integrity check.")
            if saved.execute("PRAGMA foreign_key_check").fetchall():
                raise BackupError("A SQLite snapshot failed its foreign-key check.")
    os.chmod(destination, 0o600)


def registered_paths(catalog):
    """Use catalog registrations, including retained historical publications, as authority."""
    result = []
    with closing(sqlite3.connect(Path(catalog).as_uri() + "?mode=ro", uri=True)) as connection:
        for table, identifier, prefix in (
            ("snapshots", "snapshot_id", "snapshots"),
            ("model_snapshots", "model_id", "models"),
        ):
            for identity, relative in connection.execute(
                f"SELECT {identifier},relative_path FROM {table} ORDER BY {identifier}"
            ):
                if (
                    not re.fullmatch(r"[0-9a-f]{32}", identity)
                    or relative != f"{prefix}/{identity}"
                ):
                    raise BackupError("The catalog contains an unsafe publication path.")
                result.append(relative)
    return result


def validate_publications(data, catalog):
    """Check catalog-manifest bindings and physical file hashes, not directory discovery."""
    with closing(sqlite3.connect(Path(catalog).as_uri() + "?mode=ro", uri=True)) as connection:
        for table in ("snapshots", "model_snapshots"):
            for relative, manifest_json, expected in connection.execute(
                f"SELECT relative_path,manifest_json,manifest_sha256 FROM {table}"
            ):
                directory = Path(data) / relative
                source = regular(directory / "manifest.json")
                if digest(source) != expected:
                    raise BackupError("A publication manifest differs from its catalog binding.")
                manifest = json.loads(source.read_text())
                canonical = json.dumps(
                    manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                if canonical != manifest_json:
                    raise BackupError("A publication manifest has inconsistent catalog metadata.")
                entries = manifest.get("routes", manifest.get("datasets", {}))
                for info in entries.values():
                    name = info.get("file", "")
                    if not re.fullmatch(r"[a-zA-Z0-9_.-]+\.parquet", name):
                        raise BackupError("A publication contains an unsafe data filename.")
                    if digest(regular(directory / name)) != info["sha256"]:
                        raise BackupError("A published data file differs from its manifest.")


def capture(data, state, config, release, stage, *, secrets="/run/arkham-secrets/current"):
    """Seal state and the live signing key; exclude API and bootstrap credentials.

    The caller holds deployment/writer locks. The Key Vault helper materializes secrets
    on tmpfs before this operation; capture never contacts the vault or reads legacy
    environment files. Only the Django signing key is needed to preserve signed state
    in an isolated recovery. Its staged copy is protected and encrypted with the archive.
    """
    data, state, config, release, stage = map(Path, (data, state, config, release, stage))
    if not re.fullmatch(r"[0-9a-f]{40}", release.name):
        raise BackupError("The source release must be a full commit SHA directory.")
    require_space(stage)
    stage.mkdir(mode=0o700, parents=False, exist_ok=False)
    files = {}

    def include(source, relative, *, immutable=False, database=False):
        regular(source)
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if database:
            sqlite_snapshot(source, target)
        elif immutable:
            try:
                require_space(target)
                os.link(source, target)
            except OSError:
                copy_with_reserve(source, target)
        else:
            copy_with_reserve(source, target)
        files[relative] = {"bytes": target.stat().st_size, "sha256": digest(target)}
        if len(files) > MAX_FILES or sum(item["bytes"] for item in files.values()) > MAX_BYTES:
            raise BackupError("The backup exceeds its configured file/size safety limit.")

    include(state / "web.sqlite3", "state/web.sqlite3", database=True)
    if (state / ".production-initialized").exists():
        include(state / ".production-initialized", "state/.production-initialized")
    else:
        marker = stage / "state/.production-initialized"
        marker.touch(mode=0o600)
        files["state/.production-initialized"] = {"bytes": 0, "sha256": digest(marker)}
    include(config / "deployment.json", "config/deployment.json")
    include(config / "key-vault.json", "config/key-vault.json")
    include(release / "release.json", "release/release.json")
    # The root-managed `current` link is the one deliberate symlink boundary. Resolve
    # it once, then apply the ordinary no-link file contract inside that generation.
    secret_directory = Path(secrets).resolve(strict=True)
    signing_key = regular(secret_directory / "app/django-secret-key")
    if not 1 <= signing_key.stat().st_size <= 16384:
        raise BackupError("The runtime signing key has an invalid size.")
    include(signing_key, "config/django-secret-key")
    release_config = json.loads((stage / "release/release.json").read_text())
    if set(release_config) != {"image"}:
        raise BackupError("The release metadata has unexpected fields.")
    image = release_config["image"]
    if not re.fullmatch(re.escape(IMAGE_NAMESPACE) + r"[0-9a-f]{64}", image):
        raise BackupError("The release image is not an immutable GHCR digest.")
    for name in ("compose.production.yaml", "Caddyfile", "app-control.py", "verify-state.py"):
        include(release / "deploy" / name, "release/deploy/" + name)
    catalog = data / "catalog.sqlite3"
    if catalog.exists():
        include(catalog, "data/catalog.sqlite3", database=True)
        for relative in registered_paths(stage / "data/catalog.sqlite3"):
            source = data / relative
            if source.is_symlink() or not source.is_dir() or source.resolve() != source:
                raise BackupError("A registered publication directory is unsafe or missing.")
            for item in sorted(source.rglob("*")):
                if item.is_symlink():
                    raise BackupError("Published data cannot contain symbolic links.")
                if item.is_file():
                    name = "data/" + item.relative_to(data).as_posix()
                    include(item, name, immutable=item.suffix == ".parquet")
        runs = data / "runs"
        if runs.exists():
            for item in sorted(runs.rglob("*")):
                if item.is_symlink():
                    raise BackupError("Run metadata cannot contain symbolic links.")
                if item.is_file():
                    include(item, "data/" + item.relative_to(data).as_posix())
        validate_publications(stage / "data", stage / "data/catalog.sqlite3")
    manifest = {
        "format": 2,
        "created": int(time.time()),
        "revision": release.name,
        "image": image,
        "files": files,
        "databases": {
            "web": fingerprint(stage / "state/web.sqlite3"),
            "catalog": fingerprint(stage / "data/catalog.sqlite3"),
        },
        "excluded": ["bootstrap_passwords", "eia_key", "locks", "unfinished_candidates"],
    }
    path = stage / "manifest.json"
    encoded = json.dumps(manifest, sort_keys=True).encode()
    if len(encoded) > MAX_MANIFEST:
        raise BackupError("The sealed manifest exceeds its restore safety limit.")
    write_with_reserve(path, encoded)
    return manifest


class CheckedReader:
    """Detect changes to hard-linked immutable data against the quiesced sealed hash."""

    def __init__(self, stream):
        self.stream = stream
        self.hash = hashlib.sha256()

    def read(self, size=-1):
        value = self.stream.read(size)
        self.hash.update(value)
        return value


def encrypted_stream(stage, recipient, consume, *, age="age"):
    if not re.fullmatch(r"age1[a-z0-9]{58}", recipient):
        raise BackupError("A native age public recipient is required.")
    stage = Path(stage)
    manifest_bytes = regular(stage / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    failures = []
    process = subprocess.Popen(
        [age, "--encrypt", "--recipient", recipient],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    def produce():
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|gz") as archive:
                info = tarfile.TarInfo("manifest.json")
                info.size, info.mode = len(manifest_bytes), 0o600
                archive.addfile(info, io.BytesIO(manifest_bytes))
                for name, expected in sorted(manifest["files"].items()):
                    path = regular(stage / name)
                    if path.stat().st_size != expected["bytes"]:
                        raise BackupError("A staged file changed after the backup point.")
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = expected["bytes"], 0o600
                    with path.open("rb") as source:
                        checked = CheckedReader(source)
                        archive.addfile(info, checked)
                        if checked.hash.hexdigest() != expected["sha256"]:
                            raise BackupError("A staged file changed after the backup point.")
        except Exception as error:
            failures.append(error)
        finally:
            process.stdin.close()

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    try:
        result = consume(process.stdout)
        producer.join(timeout=120)
        if producer.is_alive():
            raise BackupError("The encryption producer did not finish.")
        if process.wait(timeout=30) != 0 or failures:
            raise BackupError("Backup encryption or sealed-file verification failed.")
        return result
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        process.stdout.close()


def configuration(path):
    value = json.loads(regular(path).read_text())
    if set(value) != {"account", "container", "recipient"}:
        raise BackupError("The backup configuration has unexpected fields.")
    if not re.fullmatch(r"[a-z0-9]{3,24}", value["account"]):
        raise BackupError("Invalid storage account name.")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])", value["container"]):
        raise BackupError("Invalid private container name.")
    if not re.fullmatch(r"age1[a-z0-9]{58}", value["recipient"]):
        raise BackupError("Invalid age public recipient.")
    return value


class Blob:
    """Use only the host's managed identity; credentials never appear in URLs or logs."""

    def __init__(self, account, container):
        self.base = f"https://{account}.blob.core.windows.net/{container}/"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.token = None
        self.expires = 0

    def request(self, method, name, *, query="", body=None, headers=None):
        if not re.fullmatch(r"(?:backups|drills)/[a-zA-Z0-9_.-]+\.tar\.gz\.age", name):
            raise BackupError("Invalid backup object name.")
        for attempt in range(5):
            if self.expires < time.time() + 120:
                request = urllib.request.Request(
                    "http://169.254.169.254/metadata/identity/oauth2/token"
                    "?api-version=2018-02-01&resource=https%3A%2F%2Fstorage.azure.com%2F",
                    headers={"Metadata": "true"},
                )
                with self.opener.open(request, timeout=10) as response:
                    raw = response.read(16385)
                    if len(raw) > 16384:
                        raise BackupError("The identity response exceeded its safety limit.")
                    token = json.loads(raw)
                self.token, self.expires = token["access_token"], int(token["expires_on"])
                if not isinstance(self.token, str) or not self.token or self.expires <= time.time():
                    raise BackupError("The identity response is invalid or expired.")
            request_headers = {
                "Authorization": "Bearer " + self.token,
                "x-ms-version": "2023-11-03",
                "x-ms-date": datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT"),
                **(headers or {}),
            }
            request = urllib.request.Request(
                self.base + urllib.parse.quote(name, safe="/") + query,
                data=body,
                headers=request_headers,
                method=method,
            )
            try:
                return self.opener.open(request, timeout=90)
            except urllib.error.HTTPError as error:
                if error.code == 401:
                    self.expires = 0
                elif error.code not in {408, 429, 500, 502, 503, 504}:
                    raise BackupError(f"Blob request failed with HTTP {error.code}.") from None
                error.close()
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(min(2**attempt, 8))
        raise BackupError("Blob request exhausted its bounded retries.")

    def upload_blocks(self, name, source):
        blocks, size, checksum = [], 0, hashlib.sha256()
        while block := source.read(CHUNK):
            identity = base64.b64encode(f"{len(blocks):08d}".encode()).decode()
            query = "?comp=block&blockid=" + urllib.parse.quote(identity, safe="")
            md5 = base64.b64encode(hashlib.md5(block, usedforsecurity=False).digest()).decode()
            with self.request("PUT", name, query=query, body=block, headers={"Content-MD5": md5}):
                pass
            blocks.append(identity)
            size += len(block)
            checksum.update(block)
        return {"blocks": blocks, "bytes": size, "sha256": checksum.hexdigest()}

    def commit(self, name, uploaded, revision):
        body = (
            '<?xml version="1.0" encoding="utf-8"?><BlockList>'
            + "".join(f"<Latest>{block}</Latest>" for block in uploaded["blocks"])
            + "</BlockList>"
        ).encode()
        with self.request(
            "PUT",
            name,
            query="?comp=blocklist",
            body=body,
            headers={
                "If-None-Match": "*",
                "x-ms-blob-content-type": "application/octet-stream",
                "x-ms-meta-sha256": uploaded["sha256"],
                "x-ms-meta-revision": revision,
            },
        ):
            pass
        with self.request("HEAD", name) as response:
            if (
                int(response.headers["Content-Length"]) != uploaded["bytes"]
                or response.headers.get("x-ms-meta-sha256") != uploaded["sha256"]
                or response.headers.get("x-ms-server-encrypted", "").lower() != "true"
            ):
                raise BackupError("Remote encrypted backup verification failed.")


def upload(stage, config, *, age="age", drill_recipient=None):
    manifest = json.loads((Path(stage) / "manifest.json").read_text())
    prefix = "drills" if drill_recipient else "backups"
    name = prefix + "/" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
    name += ".tar.gz.age"
    client = Blob(config["account"], config["container"])
    uploaded = encrypted_stream(
        stage,
        drill_recipient or config["recipient"],
        lambda source: client.upload_blocks(name, source),
        age=age,
    )
    # A truncated/changed archive never gets a committed object or success receipt.
    require_space(stage, 4096)
    client.commit(name, uploaded, manifest["revision"])
    return {
        "created": int(time.time()),
        "blob": name,
        "bytes": uploaded["bytes"],
        "sha256": uploaded["sha256"],
        "revision": manifest["revision"],
        "account": config["account"],
        "container": config["container"],
        "drill": bool(drill_recipient),
    }


def check(config, receipt_path, max_age):
    receipt = json.loads(regular(receipt_path).read_text())
    if receipt.get("drill") or not 0 <= time.time() - receipt["created"] <= max_age:
        raise BackupError("The latest production backup is missing or stale.")
    if any(receipt[key] != config[key] for key in ("account", "container")):
        raise BackupError("The backup receipt belongs to different storage.")
    with Blob(config["account"], config["container"]).request("HEAD", receipt["blob"]) as response:
        if (
            int(response.headers["Content-Length"]) != receipt["bytes"]
            or response.headers.get("x-ms-meta-sha256") != receipt["sha256"]
            or response.headers.get("x-ms-server-encrypted", "").lower() != "true"
        ):
            raise BackupError("The latest remote backup does not match its success receipt.")
    return {"backup": "fresh", "age_seconds": int(time.time() - receipt["created"])}


def write_receipt(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".next")
    write_with_reserve(temporary, json.dumps(value, sort_keys=True).encode())
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capture", "upload", "check"])
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--config", type=Path, default=Path("/etc/arkham/backup.json"))
    parser.add_argument(
        "--receipt", type=Path, default=Path("/var/lib/arkham-backup/last-success.json")
    )
    parser.add_argument("--max-age", type=int, default=30 * 3600)
    parser.add_argument("--age", default="age")
    parser.add_argument("--drill-recipient")
    args = parser.parse_args()
    if args.action == "capture":
        result = capture(
            Path("/srv/arkham/data"),
            Path("/srv/arkham/state"),
            Path("/etc/arkham"),
            Path("/opt/arkham/current").resolve(),
            args.stage,
        )
        print(json.dumps({"capture": "sealed", "files": len(result["files"])}))
    elif args.action == "upload":
        if args.drill_recipient and args.receipt == Path(
            "/var/lib/arkham-backup/last-success.json"
        ):
            raise BackupError("A drill must use a separate receipt.")
        result = upload(
            args.stage,
            configuration(args.config),
            age=args.age,
            drill_recipient=args.drill_recipient,
        )
        write_receipt(args.receipt, result)
        print(json.dumps(result, sort_keys=True))
    else:
        print(json.dumps(check(configuration(args.config), args.receipt, args.max_age)))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"backup": "failed", "type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
