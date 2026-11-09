"""Read narrowly scoped runtime secrets without loading dotenv files or logging values.

Production supplies read-only files populated from Key Vault and enables
OUTAGE_SECRET_FILES_ONLY. Local development may continue supplying individual process
environment values. An explicitly configured file never falls back to another source.
"""

from __future__ import annotations

import os
import stat

_MAX_SECRET_BYTES = 16_384
_SECRET_NAMES = frozenset(
    {
        "OUTAGE_DJANGO_SECRET_KEY",
        "EIA_API_KEY",
        "OUTAGE_VIEWER_PASSWORD",
        "OUTAGE_ANALYST_PASSWORD",
        "OUTAGE_ADMIN_PASSWORD",
    }
)


class SecretConfigurationError(RuntimeError):
    """Report an invalid credential source without including its path or contents."""


def _file_value(name: str, path: str) -> str:
    """Read bounded UTF-8 from a regular file, rejecting links and special devices."""
    descriptor = None
    try:
        # O_NONBLOCK prevents an unexpected FIFO from blocking before fstat can reject it.
        # O_NOFOLLOW closes the final-component symlink race on production Linux.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOINHERIT", 0)
        if not hasattr(os, "O_NOFOLLOW") and os.path.islink(path):
            raise SecretConfigurationError(f"{name} secret file could not be read securely.")
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_SECRET_BYTES:
            raise SecretConfigurationError(f"{name} secret file could not be read securely.")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(_MAX_SECRET_BYTES + 1)
        if len(data) > _MAX_SECRET_BYTES:
            raise SecretConfigurationError(f"{name} secret file could not be read securely.")
        return data.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        raise SecretConfigurationError(f"{name} secret file could not be read securely.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_secret(name: str, *, required: bool = False) -> str:
    """Resolve one credential from NAME_FILE or an explicitly allowed local environment.

    Secret-file-only mode rejects value-bearing environment settings. Ambiguous sources,
    unreadable configured files, invalid text and empty values fail closed; no file error
    causes an environment fallback. Preserve exact text so account passwords are not
    silently normalized. Consumers that historically trim API/signing keys do so themselves.
    """
    if name not in _SECRET_NAMES:
        raise SecretConfigurationError("Unsupported secret setting.")
    mode = os.environ.get("OUTAGE_SECRET_FILES_ONLY", "0")
    if mode not in {"0", "1"}:
        raise SecretConfigurationError("OUTAGE_SECRET_FILES_ONLY must be 0 or 1.")
    value = os.environ.get(name, "")
    path = os.environ.get(f"{name}_FILE", "").strip()
    if value.strip() and path:
        raise SecretConfigurationError(f"{name} must use exactly one secret source.")
    if mode == "1" and value.strip():
        raise SecretConfigurationError(f"{name} requires a secret file in production.")
    if path:
        value = _file_value(name, path)
        if not value.strip():
            raise SecretConfigurationError(f"{name} secret file must not be empty.")
    elif mode == "1":
        value = ""
    try:
        valid_size = len(value.encode("utf-8")) <= _MAX_SECRET_BYTES
    except UnicodeError:
        valid_size = False
    if "\x00" in value or not valid_size:
        raise SecretConfigurationError(f"{name} has an invalid secret value.")
    if required and not value.strip():
        raise SecretConfigurationError(f"{name} is required.")
    return value
