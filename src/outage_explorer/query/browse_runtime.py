"""Supervise trusted, structured native browse plans over one pinned Parquet file.

Callers authorize the dataset, manifest and directory containment before entry.
This adapter rejects linked paths, verifies bytes on a bounded stat-identity cache
miss and supervises a fresh child. Only the child compiler generates SQL. Native
DuckDB settings restrict that trusted SELECT path; this is not the arbitrary-SQL
WASM capability boundary or a general OS filesystem/network jail.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from outage_explorer.analytics.model_contracts import DATASETS
from outage_explorer.query.query_sandbox import (
    ASSETS,
    SandboxError,
    _capture,
    _environment,
    _kill,
    _WindowsJob,
)

MAX_INPUT_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1001
MEMORY_BYTES = 512 * 1024 * 1024
CACHE_SIZE = 128
_VERIFIED: OrderedDict[tuple, None] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CODES = {
    "query_timeout",
    "result_too_large",
    "runtime_unavailable",
    "data_integrity",
    "query_failed",
    "resource_limit",
}


class BrowseRuntimeError(Exception):
    """Expose a static browse failure category without native diagnostics or paths."""

    def __init__(self, code: str):
        self.code = code if code in _CODES else "query_failed"
        super().__init__(self.code)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    """Bind cached bytes to regular-file identity, size and mutation timestamps."""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _descriptor_identity(descriptor: int) -> tuple[int, ...]:
    """Use Windows' real metadata ChangeTime rather than Python's legacy ctime."""
    identity = _identity(os.fstat(descriptor))
    if os.name != "nt":
        return identity
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class BasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_int64),
            ("LastAccessTime", ctypes.c_int64),
            ("LastWriteTime", ctypes.c_int64),
            ("ChangeTime", ctypes.c_int64),
            ("FileAttributes", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    information = BasicInfo()
    if (
        not kernel.GetFileInformationByHandleEx(
            wintypes.HANDLE(msvcrt.get_osfhandle(descriptor)),
            0,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        or information.ChangeTime <= 0
    ):
        raise OSError("Selected file change time is unavailable.")
    changed_ns = (information.ChangeTime - 116444736000000000) * 100
    return (*identity[:4], changed_ns)


def _path_matches(path: Path, identity: tuple[int, ...]) -> bool:
    """Compare path identity while retaining descriptor change time in cache keys.

    Windows path stat and descriptor stat expose different ctime meanings on some
    Python versions. Compare their stable device/inode/size/mtime fields; the held
    Windows handle denies write/delete sharing, and descriptor ctime is still
    checked before/after hashing and execution.
    """
    actual = _identity(path.lstat())
    return actual[:4] == identity[:4] if os.name == "nt" else actual == identity


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BrowseRuntimeError("query_timeout")
    return remaining


def _safe_path(path: Path, deadline: float) -> Path:
    """Reject relative/traversal/link paths before opening the selected regular file."""
    if not path.is_absolute() or ".." in path.parts:
        raise BrowseRuntimeError("data_integrity")
    for item in reversed((path, *path.parents)):
        _remaining(deadline)
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BrowseRuntimeError("data_integrity")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise BrowseRuntimeError("data_integrity")
    return path.resolve(strict=True)


def _open_source(path: Path) -> int:
    """Pin a read-only file, denying Windows write/delete sharing for its lifetime."""
    if os.name != "nt":
        return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "Selected file cannot be pinned.")
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def _verify_source(path: Path, descriptor: int, expected: str, deadline: float) -> tuple[int, ...]:
    """Hash once per cached identity while serializing cache misses within the deadline.

    The cache trusts host stat identity and immutable published files. It is not a
    defense against a host writer that can restore every fingerprint field. Holding
    the descriptor, rechecking it and the path, and pinning it into POSIX children
    protect ordinary replacement races. No file bytes are retained in the cache.
    """
    info = os.fstat(descriptor)
    identity = _descriptor_identity(descriptor)
    if not stat.S_ISREG(info.st_mode) or not _path_matches(path, identity):
        raise BrowseRuntimeError("data_integrity")
    key = (os.path.normcase(str(path)), expected, identity)
    if not _CACHE_LOCK.acquire(timeout=_remaining(deadline)):
        raise BrowseRuntimeError("query_timeout")
    try:
        if key not in _VERIFIED:
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                _remaining(deadline)
                digest.update(chunk)
            if digest.hexdigest() != expected:
                raise BrowseRuntimeError("data_integrity")
            if _descriptor_identity(descriptor) != identity or not _path_matches(path, identity):
                raise BrowseRuntimeError("data_integrity")
            for previous in list(_VERIFIED):
                if previous[0] == key[0]:
                    del _VERIFIED[previous]
            _VERIFIED[key] = None
            while len(_VERIFIED) > CACHE_SIZE:
                _VERIFIED.popitem(last=False)
        else:
            _VERIFIED.move_to_end(key)
    finally:
        _CACHE_LOCK.release()
    os.lseek(descriptor, 0, os.SEEK_SET)
    return identity


def run_browse(
    path: Path,
    dataset_id: str,
    plan: dict[str, Any],
    *,
    limit: int,
    last: list[str | None] | None = None,
    expected_sha256: str,
    timeout_seconds: float = 8,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Run one compiler-validated structured browse plan within a shared wall budget.

    Return declared column descriptors and at most ``limit + 1`` typed rows. The
    cold integrity hash, interpreter startup and query share the supplied budget;
    cache hits still check identity before and after execution. The caller owns
    admission and role checks. Raw SQL is not an accepted protocol field.
    """
    started = time.monotonic()
    if (
        not isinstance(dataset_id, str)
        or dataset_id not in DATASETS
        or type(limit) is not int
        or not 1 <= limit < MAX_ROWS
        or not isinstance(plan, dict)
        or not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 8
        or type(max_output_bytes) is not int
        or not 256 <= max_output_bytes <= MAX_OUTPUT_BYTES
    ):
        raise BrowseRuntimeError("query_failed")
    deadline = started + timeout_seconds
    descriptor = None
    try:
        source = _safe_path(Path(path), deadline)
        descriptor = _open_source(source)
        identity = _verify_source(source, descriptor, expected_sha256, deadline)
        environment = _environment()
        executable = sys.executable
        if os.name == "nt":
            executable = sys._base_executable
            environment["__PYVENV_LAUNCHER__"] = sys.executable
        elif sys.platform == "linux":
            # One glibc arena avoids per-thread address reservations competing
            # with the native worker's hard 512 MiB address-space ceiling.
            environment["MALLOC_ARENA_MAX"] = "1"
        payload = json.dumps(
            {
                "path": str(source),
                "identity": identity,
                "fd": descriptor if os.name != "nt" else None,
                "dataset_id": dataset_id,
                "plan": plan,
                "limit": limit,
                "last": last,
                "max_output_bytes": max_output_bytes,
                "timeout_ms": max(1, int(_remaining(deadline) * 1000)),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_INPUT_BYTES:
            raise BrowseRuntimeError("query_failed")
        args = [executable, "-I", "-B", "-m", "outage_explorer.query.browse_worker"]
        if os.name != "nt":
            args = [sys.executable, "-I", "-S", str(ASSETS / "browse_launcher.py"), *args]
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=ASSETS,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            **({"pass_fds": (descriptor,)} if os.name != "nt" else {}),
        )
        job = None
        try:
            if os.name == "nt":
                job = _WindowsJob(process)
            output, status = _capture(process, payload, _remaining(deadline), max_output_bytes)
        finally:
            _kill(process)
            process.wait()
            if job is not None:
                job.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
        _safe_path(source, deadline)
        if _descriptor_identity(descriptor) != identity or not _path_matches(source, identity):
            raise BrowseRuntimeError("data_integrity")
        try:
            response = json.loads(output)
        except (ValueError, UnicodeError):
            raise BrowseRuntimeError("resource_limit" if status else "query_failed") from None
        if not isinstance(response, dict):
            raise BrowseRuntimeError("query_failed")
        if "error" in response:
            raise BrowseRuntimeError(response["error"])
        columns = [{"name": name, "type": kind} for name, kind in DATASETS[dataset_id].columns]
        if (
            status
            or response.get("columns") != columns
            or not isinstance(response.get("rows"), list)
        ):
            raise BrowseRuntimeError("resource_limit" if status else "query_failed")
        rows = response["rows"]
        if len(rows) > limit + 1 or any(
            not isinstance(row, list) or len(row) != len(columns) for row in rows
        ):
            raise BrowseRuntimeError("query_failed")
        return {"columns": columns, "rows": rows}
    except SandboxError as error:
        raise BrowseRuntimeError(
            {"sandbox_unavailable": "runtime_unavailable"}.get(error.code, error.code)
        ) from None
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        raise BrowseRuntimeError(
            "data_integrity" if descriptor is None else "query_failed"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
