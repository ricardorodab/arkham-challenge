"""Download and verify an Arkham backup exclusively into a new disposable restore path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

import backup


def target_path(target, base):
    target, base = Path(target).absolute(), Path(base).absolute()
    if not base.exists() or base.is_symlink() or base.resolve() != base:
        raise backup.BackupError("The disposable restore base must be a real existing directory.")
    if target.exists() or target.is_symlink() or target.parent.resolve() != base:
        raise backup.BackupError("Restore requires a new direct child of its disposable base.")
    forbidden = tuple(map(Path, ("/srv/arkham", "/etc/arkham", "/opt/arkham"))) + (
        Path(__file__).resolve().parent.parent,
    )
    if any(target == path or path in target.parents for path in forbidden):
        raise backup.BackupError("Production directories cannot be restore targets.")
    return target


def manifest_contract(manifest):
    if manifest.get("format") != 1 or not re.fullmatch(r"[0-9a-f]{40}", manifest["revision"]):
        raise backup.BackupError("Unsupported backup manifest.")
    if not re.fullmatch(
        re.escape(backup.IMAGE_NAMESPACE) + r"[0-9a-f]{64}", manifest.get("image", "")
    ):
        raise backup.BackupError("The restored image is outside the trusted Arkham repository.")
    files = manifest["files"]
    if not isinstance(files, dict) or not 1 <= len(files) <= backup.MAX_FILES:
        raise backup.BackupError("Invalid backup file count.")
    size = 0
    for name, info in files.items():
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or str(path) != name
            or any(part in {".", ".."} for part in path.parts)
            or "\\" in name
            or ":" in name
            or path.parts[0] not in {"state", "data", "config", "release"}
            or not isinstance(info["bytes"], int)
            or not 0 <= info["bytes"] <= backup.MAX_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", info["sha256"])
        ):
            raise backup.BackupError("An archive member violates the safe restore contract.")
        size += info["bytes"]
    if (
        size > backup.MAX_BYTES
        or "state/web.sqlite3" not in files
        or "config/runtime.env" not in files
    ):
        raise backup.BackupError("The backup size or required file set is invalid.")
    return files


def verify(target):
    target = Path(target)
    manifest = json.loads(backup.regular(target / "manifest.json").read_text())
    files = manifest_contract(manifest)
    for name, info in files.items():
        source = backup.regular(target / name)
        if source.stat().st_size != info["bytes"] or backup.digest(source) != info["sha256"]:
            raise backup.BackupError("A restored file does not match its sealed fingerprint.")
    for name, relative in (("web", "state/web.sqlite3"), ("catalog", "data/catalog.sqlite3")):
        database = target / relative
        if backup.fingerprint(database) != manifest["databases"][name]:
            raise backup.BackupError("Restored database rows differ from the backup point.")
        if database.exists():
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise backup.BackupError("Restored SQLite integrity failed.")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise backup.BackupError("Restored SQLite references failed.")
            finally:
                connection.close()
    catalog = target / "data/catalog.sqlite3"
    if catalog.exists():
        for relative in backup.registered_paths(catalog):
            if not (target / "data" / relative / "manifest.json").is_file():
                raise backup.BackupError("A restored catalog publication is missing.")
        backup.validate_publications(target / "data", catalog)
    return {
        "restore": "verified",
        "revision": manifest["revision"],
        "files": len(files),
        "accounts": manifest["databases"]["web"]["tables"].get("auth_user", {}).get("rows", 0),
        "catalog": manifest["databases"]["catalog"]["exists"],
    }


def restore(archive, identity, target, base, *, age="age"):
    target = target_path(target, base)
    backup.regular(archive)
    backup.regular(identity)
    backup.require_space(target)
    target.mkdir(mode=0o700)
    (target / ".incomplete").touch(mode=0o600)
    process = subprocess.Popen(
        [age, "--decrypt", "--identity", str(identity), str(archive)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|gz") as incoming:
            first = incoming.next()
            if (
                first is None
                or first.name != "manifest.json"
                or not first.isfile()
                or first.size > CHUNK_LIMIT
            ):
                raise backup.BackupError("The archive must begin with a bounded sealed manifest.")
            raw = incoming.extractfile(first).read()
            manifest = json.loads(raw)
            files = manifest_contract(manifest)
            remaining = sum(info["bytes"] for info in files.values())
            backup.require_space(target, remaining + len(raw))
            backup.write_with_reserve(target / "manifest.json", raw)
            seen = set()
            # Iteration starts with the cached first member; it is handled above only.
            for member in incoming:
                if member is first:
                    continue
                if member.name not in files or member.name in seen or not member.isfile():
                    raise backup.BackupError("Unexpected, duplicate or linked archive member.")
                expected = files[member.name]
                if member.size != expected["bytes"]:
                    raise backup.BackupError("An archive member has the wrong size.")
                destination = target / member.name
                backup.require_space(target, remaining)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                checksum = hashlib.sha256()
                with incoming.extractfile(member) as source, destination.open("xb") as output:
                    while block := source.read(backup.CHUNK):
                        backup.require_space(target, remaining)
                        output.write(block)
                        checksum.update(block)
                        remaining -= len(block)
                os.chmod(destination, 0o600)
                if checksum.hexdigest() != expected["sha256"]:
                    raise backup.BackupError("An archive member failed its sealed checksum.")
                seen.add(member.name)
            if seen != set(files):
                raise backup.BackupError("The archive is incomplete.")
        # Consume authenticated stream termination; no partially decrypted tree is runnable.
        while process.stdout.read(backup.CHUNK):
            pass
        if process.wait(timeout=30) != 0:
            raise backup.BackupError("The encrypted archive failed authentication.")
        result = verify(target)
        (target / ".incomplete").unlink()
        (target / ".verified").touch(mode=0o600)
        return result
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        process.stdout.close()


CHUNK_LIMIT = backup.MAX_MANIFEST


def download(config, blob, output):
    client = backup.Blob(config["account"], config["container"])
    output = Path(output)
    with client.request("HEAD", blob) as response:
        expected_size = int(response.headers["Content-Length"])
        expected_hash = response.headers["x-ms-meta-sha256"]
    if not 1 <= expected_size <= backup.MAX_BYTES + backup.MAX_FILES * 1024 + CHUNK_LIMIT:
        raise backup.BackupError("The encrypted object exceeds its download safety limit.")
    backup.require_space(output, expected_size)
    checksum, size = hashlib.sha256(), 0
    with output.open("xb") as destination, client.request("GET", blob) as source:
        os.chmod(output, 0o600)
        while block := source.read(backup.CHUNK):
            if size + len(block) > expected_size:
                raise backup.BackupError("The download exceeds its declared size.")
            backup.require_space(output, expected_size - size)
            destination.write(block)
            checksum.update(block)
            size += len(block)
    if size != expected_size or checksum.hexdigest() != expected_hash:
        raise backup.BackupError("The encrypted download checksum failed.")
    return {"download": "verified", "bytes": size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["download", "restore", "verify"])
    parser.add_argument("--config", type=Path, default=Path("/etc/arkham/backup.json"))
    parser.add_argument("--blob")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--base", type=Path, default=Path("/srv/arkham-restore-tests"))
    parser.add_argument("--age", default="age")
    args = parser.parse_args()
    if args.action == "download":
        result = download(backup.configuration(args.config), args.blob, args.archive)
    elif args.action == "restore":
        result = restore(args.archive, args.identity, args.target, args.base, age=args.age)
    else:
        if not (args.target / ".verified").is_file() or (args.target / ".incomplete").exists():
            raise backup.BackupError("The disposable restore has not completed authentication.")
        result = verify(args.target)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"restore": "failed", "type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
