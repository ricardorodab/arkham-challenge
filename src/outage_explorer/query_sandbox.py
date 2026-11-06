"""Supervise disposable DuckDB-WASM executions over one verified Parquet buffer.

The service must authorize the dataset and compile the SQL before calling this
module. This layer checks the trusted runtime, hashes the selected file, sends a
bounded header/binary payload to a fresh Node child, and supervises resources and
pipes. It does not establish the caller's persona or validate SQL grammar.

Node permission flags restrict trusted JavaScript bootstrap reads; the engine's
BUFFER-only WASM imports are the file/network capability boundary. Windows Job
Objects and the POSIX resource bootstrap are separate resource controls. The
native DuckDB connection used by trusted ingestion/reports is not used here.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

PACKAGE_VERSION = "1.33.1-dev57.0"
ENGINE_VERSION = "v1.5.4"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1001  # one extra row lets the API report truncation at its 1000 cap
MEMORY_BYTES = 512 * 1024 * 1024
ASSETS = Path(__file__).resolve().parent / "sandbox"
EXTENSION_SHA256 = "4845705bbd69fc9ad52878d96a505c73cae4a6c509822079cc2413e5eb437f95"
_MESSAGES = {
    "sandbox_unavailable": "The isolated query runtime is unavailable.",
    "invalid_request": "The query execution request is invalid.",
    "source_changed": "The selected dataset failed its integrity check.",
    "query_timeout": "The query exceeded its execution deadline.",
    "resource_limit": "The query exceeded its process resource limit.",
    "result_too_large": "The query result exceeds the response size limit.",
    "query_failed": "The query could not be executed within the supported contract.",
    "non_finite_result": "The query returned a non-finite number.",
    "unsupported_result_type": "The query returned an unsupported result type.",
}


class SandboxError(Exception):
    """Represent a bounded public execution error without exposing child diagnostics.

    Unknown codes collapse to ``query_failed``; only the static message table is
    permitted to cross the process boundary into service error handling.
    """

    def __init__(self, code: str):
        """Normalize ``code`` and attach its static, credential-free public message."""
        self.code = code if code in _MESSAGES else "query_failed"
        self.message = _MESSAGES[self.code]
        super().__init__(self.message)


def _runtime() -> tuple[str, Path]:
    """Resolve the pinned npm engine installation and a supported Node executable.

    An explicit ``OUTAGE_SANDBOX_NODE_MODULES`` replaces automatic candidates.
    Package metadata and required asset presence are checked here; the extension's
    content hash is checked separately. Raise ``sandbox_unavailable`` on absence or
    version mismatch. Operator-selected paths and installed JavaScript are trusted.
    """
    node = shutil.which("node")
    configured = os.environ.get("OUTAGE_SANDBOX_NODE_MODULES")
    candidates = (
        [Path(configured)]
        if configured
        else [Path.cwd() / "node_modules", Path(__file__).resolve().parents[2] / "node_modules"]
    )
    if not node:
        raise SandboxError("sandbox_unavailable")
    for candidate in candidates:
        modules = candidate.resolve()
        package = modules / "@duckdb" / "duckdb-wasm" / "package.json"
        try:
            version = json.loads(package.read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        required = [
            ASSETS / "worker.cjs",
            ASSETS / "parquet-v1.5.4-wasm_eh.wasm",
            modules / "@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs",
            modules / "@duckdb/duckdb-wasm/dist/duckdb-eh.wasm",
        ]
        if os.name != "nt":
            required.append(ASSETS / "posix_launcher.py")
        if version == PACKAGE_VERSION and all(path.is_file() for path in required):
            executable = str(Path(node).resolve())
            _node_version(executable)
            return executable, modules
    raise SandboxError("sandbox_unavailable")


def _environment() -> dict[str, str]:
    # No PATH/NODE_OPTIONS, HOME, EIA key, Django key, database URL or cloud
    # credentials. Absolute executable and package paths remove PATH lookup.
    """Build the minimal child environment without inherited application secrets.

    Only locale/timezone/warning settings and required Windows runtime variables
    are retained. Absolute executable and package paths avoid child PATH lookup.
    """
    environment = {"TZ": "UTC", "LANG": "C.UTF-8", "NODE_NO_WARNINGS": "1"}
    if os.name == "nt":
        for name in ("SystemRoot", "WINDIR"):
            if name in os.environ:
                environment[name] = os.environ[name]
    return environment


@lru_cache(maxsize=2)
def _node_version(node: str) -> str:
    """Return a cached supported Node version after a bounded, scrubbed probe.

    Require major 24 and minor at least 13. Process, decoding and version failures
    raise ``sandbox_unavailable``; this check does not execute an analytical query.
    """
    try:
        result = subprocess.run(
            [node, "--version"], capture_output=True, timeout=2, env=_environment(), check=True
        )
        version = result.stdout.decode("ascii").strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise SandboxError("sandbox_unavailable") from None
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", version)
    if not match or int(match[1]) != 24 or int(match[2]) < 13:
        raise SandboxError("sandbox_unavailable")
    return version


def runtime_status() -> dict[str, Any]:
    """Describe pinned runtime availability without running submitted SQL.

    Validate package/Node resolution and the packaged Parquet extension checksum.
    Expose platform-specific resource descriptions, not a completed query or proof
    of equivalent POSIX runtime acceptance. Known probe failures become a bounded
    ``available: False`` result.
    """
    try:
        node, _ = _runtime()
        with (ASSETS / "parquet-v1.5.4-wasm_eh.wasm").open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        if checksum != EXTENSION_SHA256:
            raise SandboxError("sandbox_unavailable")
        return {
            "available": True,
            "node": _node_version(node),
            "package": PACKAGE_VERSION,
            "engine": ENGINE_VERSION,
            "isolation": "wasm-buffer-v1",
            "process_memory_limit_mib": 512 if os.name == "nt" else None,
            "data_allocation_limit_mib": (1024 if sys.platform == "linux" else 512)
            if os.name != "nt"
            else None,
            "wasm_memory_limit_mib": 256,
        }
    except (SandboxError, OSError, subprocess.SubprocessError, UnicodeError):
        return {"available": False, "isolation": "wasm-buffer-v1"}


def is_available() -> bool:
    """Return the runtime probe's availability flag, without an engine query."""
    return bool(runtime_status()["available"])


class _WindowsJob:
    """Own Windows child memory/CPU/process limits and kill-on-close behavior.

    The Job Object caps process and job memory at 512 MiB, user CPU at six seconds,
    and active processes at one. These OS limits are distinct from the engine's
    linear-memory/V8 settings and the supervisor's wall-clock deadline.
    """

    def __init__(self, process: subprocess.Popen[bytes]):
        """Create a Job Object, configure its limits and assign the spawned child.

        The local ctypes structures reproduce the Windows ABI. Failure closes the
        handle and raises ``sandbox_unavailable``; the caller still owns process reaping.
        """
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            """Match JOBOBJECT_BASIC_LIMIT_INFORMATION field order and native widths."""

            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            """Match the six unsigned 64-bit Windows IO_COUNTERS fields."""

            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            """Match JOBOBJECT_EXTENDED_LIMIT_INFORMATION, including memory counters."""

            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.CreateJobObjectW(None, None)
        self._kernel, self._handle = kernel, handle
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x100 | 0x200 | 0x8 | 0x2
        limits.BasicLimitInformation.ActiveProcessLimit = 1
        limits.BasicLimitInformation.PerProcessUserTimeLimit = 6 * 10_000_000
        limits.ProcessMemoryLimit = limits.JobMemoryLimit = MEMORY_BYTES
        if (
            not handle
            or not kernel.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            )
            or not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle)))
        ):
            self.close()
            raise SandboxError("sandbox_unavailable")

    def close(self) -> None:
        """Close the owned Job handle once; kill-on-close also terminates its child."""
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _kill(process: subprocess.Popen[bytes]) -> None:
    """Terminate a live child, or its POSIX process group, without reaping it.

    A disappearing POSIX process group is harmless. Callers must still wait for the
    process and release handles/pipes after this best-effort termination step.
    """
    if process.poll() is None:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _capture(
    process: subprocess.Popen[bytes], payload: bytes, timeout: float, limit: int
) -> tuple[bytes, int]:
    """Write input and drain bounded child pipes while enforcing a wall deadline.

    Stdout is retained; stderr is counted but discarded. Either pipe exceeding
    ``limit`` terminates the child. Timeout raises ``query_timeout`` and overflow
    raises ``result_too_large``. All outcomes wait for the process, join I/O helpers
    with bounded waits, and close pipes; successful capture returns bytes/status.
    """
    output = bytearray()
    overflow = threading.Event()

    def read(stream: Any, keep: bool) -> None:
        """Drain one pipe, retaining only stdout while bounding both output streams.

        Signal overflow and kill the child before unbounded output can accumulate.
        Private stderr is deliberately discarded even when it remains within the cap.
        """
        count = 0
        try:
            while chunk := stream.read(65536):
                count += len(chunk)
                if count > limit:
                    overflow.set()
                    _kill(process)
                    return
                if keep:
                    output.extend(chunk)
        except OSError:
            return

    def write() -> None:
        """Feed the complete framed payload and close stdin to delimit the request.

        A child that exits early may break the pipe; its status/output remains the
        supervisor's source of the execution outcome.
        """
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    threads = [
        threading.Thread(target=read, args=(process.stdout, True), daemon=True),
        threading.Thread(target=read, args=(process.stderr, False), daemon=True),
        threading.Thread(target=write, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=max(timeout, 0.001))
    except subprocess.TimeoutExpired:
        _kill(process)
        process.wait()
        raise SandboxError("query_timeout") from None
    finally:
        if process.poll() is None:
            _kill(process)
            process.wait()
        for thread in threads:
            thread.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    if overflow.is_set():
        raise SandboxError("result_too_large")
    return bytes(output), process.returncode


def run_query(
    parquet_path: Path,
    dataset_id: str,
    sql: str,
    *,
    expected_sha256: str,
    timeout_seconds: float = 8,
    max_rows: int = MAX_ROWS,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Execute already-compiled SQL against one checksum-verified dataset file.

    Args:
        parquet_path: Server-selected immutable file; never supplied to the child.
        dataset_id: Authorized logical view name created by trusted bootstrap.
        sql: Regenerated SQL whose grammar and references the caller has checked.
        expected_sha256: Published digest binding the file bytes to trusted metadata.
        timeout_seconds: Remaining wall budget, including runtime/input preparation.
        max_rows: Engine row ceiling, optionally including the caller's sentinel.
        max_output_bytes: Upper bound for the child response and captured diagnostics.

    Returns:
        The bounded typed response after protocol/status validation. Decimal and
        large-integer encodings are produced by the JavaScript worker.

    Raises:
        SandboxError: Invalid bounds, changed source, unavailable runtime, exhausted
            resources/deadline, malformed response or a static worker error.

    The whole file is read/hashed before engine execution, up to 64 MiB. Node's
    trusted bootstrap reads only granted runtime assets; it receives no dataset
    host path or inherited application credentials. The child is killed/reaped and
    OS handles/pipes closed after every launched execution. No native fallback is
    provided, and preflight may consume a short deadline before a child starts.
    """
    started = time.monotonic()
    if (
        not isinstance(dataset_id, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", dataset_id) is None
        or not isinstance(sql, str)
        or not sql.strip()
        or len(sql.encode()) > 16384
        or not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or type(max_rows) is not int
        or not 1 <= max_rows <= MAX_ROWS
        or type(max_output_bytes) is not int
        or not 256 <= max_output_bytes <= MAX_OUTPUT_BYTES
        or type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 8
    ):
        raise SandboxError("invalid_request")
    node, modules = _runtime()
    source = Path(parquet_path)
    try:
        if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_INPUT_BYTES:
            raise SandboxError("source_changed")
        with source.open("rb") as stream:
            parquet = stream.read(MAX_INPUT_BYTES + 1)
        if len(parquet) > MAX_INPUT_BYTES or hashlib.sha256(parquet).hexdigest() != expected_sha256:
            raise SandboxError("source_changed")
    except OSError:
        raise SandboxError("source_changed") from None
    header = json.dumps(
        {
            "dataset_id": dataset_id,
            "sql": sql,
            "max_rows": max_rows,
            "max_output_bytes": max_output_bytes,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    payload = header + b"\n" + parquet
    args = [
        node,
        "--permission",
        f"--allow-fs-read={modules}",
        f"--allow-fs-read={ASSETS / 'worker.cjs'}",
        f"--allow-fs-read={ASSETS / 'parquet-v1.5.4-wasm_eh.wasm'}",
        "--disallow-code-generation-from-strings",
        "--disable-proto=throw",
        "--disable-wasm-trap-handler",
        "--max-old-space-size=96",
        "--liftoff-only",
        "--no-wasm-tier-up",
        "--wasm-num-compilation-tasks=1",
        "--wasm-max-mem-pages=4096",
        "--wasm-max-committed-code-mb=128",
        str(ASSETS / "worker.cjs"),
        str(modules),
    ]
    if os.name != "nt":
        if sys.platform == "linux":
            # Linux counts V8's writable code reservations against RLIMIT_DATA.
            # Bound WASM reservations separately from committed code; engine mode
            # also budgets the pinned Linux x64 Node's JavaScript CodeRange.
            args.insert(1, "--wasm-max-code-space-size-mb=128")
            # Avoid precommitting an oversized initial reservation for each module.
            # Code may still grow within the unchanged per-space/committed caps.
            args.insert(2, "--wasm-max-initial-code-space-reservation=32")
        args = [sys.executable, "-I", "-S", str(ASSETS / "posix_launcher.py"), "--engine", *args]
    remaining = timeout_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise SandboxError("query_timeout")
    job = None
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_environment(),
            cwd=ASSETS,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            if os.name == "nt":
                job = _WindowsJob(process)
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise SandboxError("query_timeout")
            output, status = _capture(process, payload, remaining, max_output_bytes)
        finally:
            _kill(process)
            process.wait()
            if job is not None:
                job.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
    except (OSError, ValueError):
        raise SandboxError("sandbox_unavailable") from None
    try:
        response = json.loads(output)
    except (ValueError, UnicodeError):
        raise SandboxError("resource_limit" if status else "query_failed") from None
    if not isinstance(response, dict):
        raise SandboxError("query_failed")
    if "error" in response:
        raise SandboxError(response["error"])
    if status or not isinstance(response.get("rows"), list) or len(response["rows"]) > max_rows:
        raise SandboxError("query_failed")
    return response
