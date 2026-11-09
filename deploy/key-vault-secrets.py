"""Materialize production secrets from the fixed Azure vault onto host tmpfs.

Only the VM host requests its system-managed identity token. Containers receive a
read-only directory containing their own secret files, never a token or a vault
credential. A complete immutable generation is published with one symlink change;
failed retrieval leaves the previous generation available for existing containers.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

VAULT_URL = "https://arkhamkv.vault.azure.net"
TOKEN_URL = (
    "http://169.254.169.254/metadata/identity/oauth2/token?"
    "api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net"
)
API_VERSION = "2025-07-01"
MAX_RESPONSE = 64 * 1024
MAX_SECRET = 4096
RUNTIME_UID = 10001
RUNTIME_GID = 10001
HOST_UID = 0
HOST_GID = 0
SECRET_NAMES = (
    "django-secret-key",
    "eia-api-key",
    "bootstrap-viewer-password",
    "bootstrap-analyst-password",
    "bootstrap-admin-password",
)
GENERATION = re.compile(r"generation-[0-9a-f]{32}\Z")
ROLE_FILES = {
    "app": {"django-secret-key": "django-secret-key"},
    "worker": {"django-secret-key": "django-secret-key", "eia-api-key": "eia-api-key"},
    "init": {
        "django-secret-key": "django-secret-key",
        "viewer-password": "bootstrap-viewer-password",
        "analyst-password": "bootstrap-analyst-password",
        "admin-password": "bootstrap-admin-password",
    },
}


class SecretError(RuntimeError):
    """Report fixed operator messages without secret values, tokens or response bodies."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Prevent identity tokens from following a credential-bearing redirect."""

    def redirect_request(self, request, response, code, message, headers, newurl):
        raise SecretError("Secret requests must not redirect.")


def regular_bytes(path, maximum, *, owner=None, mode=None):
    """Read one bounded regular file without following its final symlink."""
    owner = HOST_UID if owner is None else owner
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner
                or info.st_nlink != 1
                or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
                or info.st_size > maximum
            ):
                raise SecretError("A protected secret-management file is invalid.")
            value = stream.read(maximum + 1)
            if len(value) > maximum:
                raise SecretError("A protected secret-management file is too large.")
            return value
    except SecretError:
        raise
    except OSError:
        raise SecretError("A protected secret-management file is unavailable.") from None


def load_config(path):
    """Accept only the fixed vault and an explicit first-install bootstrap requirement."""
    try:
        config = json.loads(regular_bytes(path, 4096, mode=0o600))
    except (ValueError, UnicodeError):
        raise SecretError("The Key Vault configuration is invalid.") from None
    if (
        not isinstance(config, dict)
        or not {"vault_url", "bootstrap_required"}.issubset(config)
        or not set(config).issubset({"vault_url", "bootstrap_required", "django_sha256"})
        or config["vault_url"] != VAULT_URL
        or type(config["bootstrap_required"]) is not bool
        or (
            "django_sha256" in config
            and (
                not isinstance(config["django_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", config["django_sha256"])
            )
        )
    ):
        raise SecretError("The Key Vault configuration is invalid.")
    return config


def _json_response(response):
    raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise SecretError("An identity or vault response exceeded its size limit.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SecretError("An identity or vault response was invalid.") from None
    if not isinstance(value, dict):
        raise SecretError("An identity or vault response was invalid.")
    return value


class VaultClient:
    """Retrieve known secrets using an in-memory, system-managed identity token."""

    def __init__(self, opener=None, *, sleep=time.sleep, clock=time.time):
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        self.sleep = sleep
        self.clock = clock
        self._token = None
        self._expires = 0

    def request(self, request, *, optional=False):
        """Retry bounded transient failures; never expose an upstream error body."""
        for attempt in range(4):
            try:
                with self.opener.open(request, timeout=10) as response:
                    if response.status != 200:
                        raise SecretError("An identity or vault request failed.")
                    return _json_response(response)
            except urllib.error.HTTPError as error:
                code = error.code
                error.close()
                if code == 404 and optional:
                    return None
                if code not in {408, 429, 500, 502, 503, 504} or attempt == 3:
                    raise SecretError("An identity or vault request was rejected.") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 3:
                    raise SecretError("An identity or vault request was unavailable.") from None
            self.sleep(2**attempt)
        raise SecretError("An identity or vault request failed.")

    def token(self):
        """Request only the vault audience and refresh before token expiry."""
        if self._token and self._expires > self.clock() + 60:
            return self._token
        body = self.request(urllib.request.Request(TOKEN_URL, headers={"Metadata": "true"}))
        token = body.get("access_token")
        token_type = body.get("token_type")
        try:
            expires = int(body.get("expires_on", 0))
        except (TypeError, ValueError):
            expires = 0
        if (
            not isinstance(token, str)
            or not token
            or any(character.isspace() for character in token)
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or body.get("resource") != "https://vault.azure.net"
            or expires <= self.clock() + 60
        ):
            raise SecretError("The managed identity token response was invalid.")
        self._token, self._expires = token, expires
        return token

    def secret(self, name, *, required):
        """Get the latest enabled version of a fixed secret without listing the vault."""
        if name not in SECRET_NAMES:
            raise SecretError("An unknown production secret was requested.")
        request = urllib.request.Request(
            f"{VAULT_URL}/secrets/{name}?api-version={API_VERSION}",
            headers={"Authorization": "Bearer " + self.token()},
        )
        body = self.request(request, optional=not required)
        if body is None:
            return None
        value = body.get("value")
        attributes = body.get("attributes")
        expected = re.escape(f"{VAULT_URL}/secrets/{name}/")
        identifier = body.get("id", "")
        if (
            not isinstance(value, str)
            or not value
            or not value.strip()
            or len(value.encode("utf-8")) > MAX_SECRET
            or any(character in value for character in "\x00\r\n")
            or not isinstance(attributes, dict)
            or attributes.get("enabled") is not True
            or not isinstance(identifier, str)
            or not re.fullmatch(expected + r"[0-9a-f]{32}", identifier)
        ):
            raise SecretError("A production secret response was invalid.")
        for field in ("nbf", "exp"):
            timestamp = attributes.get(field)
            if timestamp is not None and (
                type(timestamp) is not int
                or (field == "nbf" and timestamp > self.clock())
                or (field == "exp" and timestamp <= self.clock())
            ):
                raise SecretError("A production secret is outside its validity period.")
        return {"value": value.encode("utf-8"), "version": identifier.rsplit("/", 1)[1]}


def fetch_values(config, client):
    """Retrieve the entire set before changing any published runtime files."""
    values = {}
    for name in SECRET_NAMES:
        required = name == "django-secret-key" or (
            name.startswith("bootstrap-") and config["bootstrap_required"]
        )
        value = client.secret(name, required=required)
        if value is not None:
            values[name] = value
    expected = config.get("django_sha256")
    if expected is not None and (
        "django-secret-key" not in values
        or not hmac.compare_digest(
            hashlib.sha256(values["django-secret-key"]["value"]).hexdigest(), expected
        )
    ):
        raise SecretError("The Django signing key does not match its production identity pin.")
    return values


def _mount_path(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def validate_root(root, *, mountinfo=Path("/proc/self/mountinfo")):
    """Require root-owned, nonlinked storage beneath /run on an actual tmpfs mount."""
    if os.name != "posix" or os.geteuid() != 0:
        raise SecretError("Production secret retrieval requires the Linux root account.")
    root = Path(root)
    if (
        not root.is_absolute()
        or ".." in root.parts
        or root == Path("/run")
        or Path("/run") not in root.parents
    ):
        raise SecretError("Runtime secrets must be stored beneath /run.")
    current = Path("/")
    for component in root.parts[1:]:
        current /= component
        if current.is_symlink():
            raise SecretError("Runtime secret paths must not contain symlinks.")
    try:
        mounts = []
        for line in mountinfo.read_text().splitlines():
            fields, filesystem = line.split(" - ", 1)
            mounted = Path(_mount_path(fields.split()[4]))
            if mounted == root or mounted in root.parents:
                mounts.append((len(mounted.parts), filesystem.split()[0]))
        if not mounts or max(mounts)[1] != "tmpfs":
            raise SecretError("Runtime secrets require a tmpfs filesystem.")
        root.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = root.stat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != HOST_UID
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise SecretError("The runtime secret directory is not private.")
    except SecretError:
        raise
    except (OSError, ValueError, IndexError):
        raise SecretError("Runtime secret storage validation failed.") from None
    return root


@contextmanager
def generation_lock(root):
    """Serialize refreshes so only one fully fetched generation can replace current."""
    import fcntl

    descriptor = os.open(
        root / ".fetch.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != HOST_UID
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise SecretError("The runtime secret lock is invalid.")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _fsync(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path, value, *, owner, group, mode):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fchown(stream.fileno(), owner, group)
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())


def _layout(values):
    return {
        role: {filename: secret for filename, secret in files.items() if secret in values}
        for role, files in ROLE_FILES.items()
    }


def _current_generation(root):
    link = root / "current"
    if not link.exists() and not link.is_symlink():
        return None
    if not link.is_symlink():
        raise SecretError("The current secret generation reference is invalid.")
    target = os.readlink(link)
    if not GENERATION.fullmatch(target):
        raise SecretError("The current secret generation reference is invalid.")
    generation = root / target
    if generation.is_symlink() or not generation.is_dir():
        raise SecretError("The current secret generation is unavailable.")
    info = generation.stat()
    if info.st_uid != HOST_UID or stat.S_IMODE(info.st_mode) != 0o700:
        raise SecretError("The current secret generation is not private.")
    return generation


def _same_generation(generation, values, versions):
    """Reuse a still-valid generation; value comparisons remain inside process memory."""
    try:
        manifest = json.loads(regular_bytes(generation / "manifest.json", 8192, mode=0o600))
        if manifest != {"schema": 1, "vault_url": VAULT_URL, "versions": versions}:
            return False
        if {path.name for path in generation.iterdir()} != {
            "app",
            "worker",
            "init",
            "manifest.json",
        }:
            return False
        for role, files in _layout(values).items():
            directory = generation / role
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != RUNTIME_UID
                or stat.S_IMODE(info.st_mode) != 0o500
                or {path.name for path in directory.iterdir()} != set(files)
            ):
                return False
            for filename, secret in files.items():
                if (
                    regular_bytes(directory / filename, MAX_SECRET, owner=RUNTIME_UID, mode=0o400)
                    != values[secret]["value"]
                ):
                    return False
        return True
    except (SecretError, OSError, ValueError, UnicodeError):
        return False


def materialize(root, values):
    """Atomically publish complete role directories without changing old mount inodes."""
    if "django-secret-key" not in values or not set(values).issubset(SECRET_NAMES):
        raise SecretError("The retrieved production secret set is incomplete.")
    versions = {name: secret["version"] for name, secret in sorted(values.items())}
    current = _current_generation(root)
    if current is not None and _same_generation(current, values, versions):
        return {"generation": current.name, "available": sorted(values), "changed": False}
    token = uuid.uuid4().hex
    candidate = root / f".candidate-{token}"
    generation = root / f"generation-{token}"
    temporary_link = root / f".current-{token}"
    candidate.mkdir(mode=0o700)
    try:
        for role, files in _layout(values).items():
            directory = candidate / role
            directory.mkdir(mode=0o700)
            for filename, secret in files.items():
                _write(
                    directory / filename,
                    values[secret]["value"],
                    owner=RUNTIME_UID,
                    group=RUNTIME_GID,
                    mode=0o400,
                )
            os.chown(directory, RUNTIME_UID, RUNTIME_GID)
            os.chmod(directory, 0o500)
            _fsync(directory)
        manifest = {"schema": 1, "vault_url": VAULT_URL, "versions": versions}
        _write(
            candidate / "manifest.json",
            json.dumps(manifest, sort_keys=True).encode(),
            owner=HOST_UID,
            group=HOST_GID,
            mode=0o600,
        )
        _fsync(candidate)
        os.rename(candidate, generation)
        os.symlink(generation.name, temporary_link)
        os.replace(temporary_link, root / "current")
        _fsync(root)
        return {"generation": generation.name, "available": sorted(values), "changed": True}
    finally:
        temporary_link.unlink(missing_ok=True)
        if candidate.exists():
            # Only this invocation's fixed UUID candidate can be removed. Published
            # generations remain for running containers and explicit operator cleanup.
            for role in ROLE_FILES:
                directory = candidate / role
                if directory.exists():
                    os.chmod(directory, 0o700)
            shutil.rmtree(candidate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["fetch"])
    parser.add_argument("--config", type=Path, default=Path("/etc/arkham/key-vault.json"))
    parser.add_argument("--root", type=Path, default=Path("/run/arkham-secrets"))
    arguments = parser.parse_args()
    root = validate_root(arguments.root)
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    config = load_config(arguments.config)
    with generation_lock(root):
        values = fetch_values(config, VaultClient())
        receipt = materialize(root, values)
    print(json.dumps({"secrets": "ready", **receipt}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except SecretError as error:
        print(json.dumps({"secrets": "unavailable", "error": str(error)}), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print(
            json.dumps({"secrets": "unavailable", "error": "Secret retrieval failed."}),
            file=sys.stderr,
        )
        sys.exit(1)
