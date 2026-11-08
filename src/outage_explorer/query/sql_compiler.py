"""Supervise bounded SQL compilation without parsing submitted SQL in Django.

The disposable Python child returns all named references before its supported-
shape result. Callers must authorize those references before raising a shape
error or opening analytical data. Only regenerated SQL can reach the separate
Node/WASM execution child; there is no in-process parser fallback.

The compiler receives scrubbed configuration and no analytical file bytes or
paths. Its OS resource limits do not make trusted Python/parser dependencies a
filesystem jail. Process/protocol failures remain static public errors.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass

from outage_explorer.query.query_sandbox import (
    ASSETS,
    SandboxError,
    _capture,
    _environment,
    _kill,
    _WindowsJob,
)
from outage_explorer.query.sql_policy import MAX_ROWS, MAX_SQL_BYTES, SQLPolicyError, ValidatedQuery

MAX_INPUT_BYTES = 32 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
ERROR_MESSAGES = {
    "invalid_sql": "SQL could not be parsed.",
    "sql_too_large": "SQL exceeds the 16 KiB input limit.",
    "sql_too_complex": "SQL exceeds the supported complexity limits.",
    "unsupported_sql": "SQL uses a feature outside the supported single-dataset SELECT subset.",
    "multiple_statements": "Exactly one SELECT statement is supported.",
    "unknown_dataset": "The selected dataset is unavailable.",
    "dataset_mismatch": "SQL references a dataset other than the selected source.",
    "unknown_column": "SQL references an unavailable column or qualifier.",
    "invalid_pagination": "Row limit or offset is outside the supported range.",
}


@dataclass(frozen=True)
class Compilation:
    """Carry discovered references independently of compilation success.

    Exactly one of ``query`` or ``error`` describes the policy result. References
    must be authorized even when ``error`` records unsupported SQL syntax.
    """

    dataset_ids: tuple[str, ...]
    query: ValidatedQuery | None
    error: SQLPolicyError | None


def _invalid(code: str = "invalid_sql") -> Compilation:
    """Create a local policy rejection with no discovered references.

    Used only for bounded preflight failures before the child parses input; public
    messages are selected from the static compiler error table.
    """
    return Compilation((), None, SQLPolicyError(ERROR_MESSAGES[code], code=code))


def _message(response, *, max_rows: int) -> Compilation:
    """Validate a child frame and reconstruct only the supported result objects.

    Require sorted unique bounded references, exact protocol keys and the requested
    row cap. Child error codes map to static messages; arbitrary diagnostics are
    never accepted. Malformed frames raise ``sandbox_unavailable`` rather than
    triggering a parent-process parser fallback.
    """
    if not isinstance(response, dict) or set(response) != {"dataset_ids", "query", "error"}:
        raise SandboxError("sandbox_unavailable")
    references = response["dataset_ids"]
    if (
        not isinstance(references, list)
        or len(references) > 512
        or any(
            not isinstance(value, str) or len(value.encode("utf-8")) > 16384 for value in references
        )
        or references != sorted(set(references))
    ):
        raise SandboxError("sandbox_unavailable")
    error = response["error"]
    query = response["query"]
    if error is not None:
        if (
            not isinstance(error, dict)
            or set(error) != {"code"}
            or error["code"] not in ERROR_MESSAGES
        ):
            raise SandboxError("sandbox_unavailable")
        if query is not None:
            raise SandboxError("sandbox_unavailable")
        code = error["code"]
        return Compilation(tuple(references), None, SQLPolicyError(ERROR_MESSAGES[code], code=code))
    if (
        not isinstance(query, dict)
        or set(query) != {"sql", "dataset_ids", "row_limit"}
        or not isinstance(query["sql"], str)
        or not query["sql"]
        or len(query["sql"].encode("utf-8")) > MAX_SQL_BYTES
        or query["dataset_ids"] != references
        or type(query["row_limit"]) is not int
        or query["row_limit"] != max_rows
    ):
        raise SandboxError("sandbox_unavailable")
    return Compilation(
        tuple(references), ValidatedQuery(query["sql"], tuple(references), max_rows), None
    )


def compile_sql(
    sql: str, dataset_id: str, *, max_rows: int = MAX_ROWS, timeout_seconds: float = 2
) -> Compilation:
    """Compile SQL in a killable child within the caller's remaining subdeadline.

    Validate UTF-8/input/cap bounds before launching isolated Python. Return a
    ``Compilation`` for policy errors, retaining discovered references for caller
    authorization. Runtime, resource, timeout and protocol failures raise
    ``SandboxError``. Startup and payload preparation consume the same at-most-two-
    second budget, so a very short budget can expire before execution begins.

    Windows bypasses the venv redirector to preserve the one-process Job limit;
    POSIX execs through its resource launcher. All launched outcomes kill/reap the
    child and release pipes/Job handles. No analytical files are read here.
    """
    started = time.monotonic()
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 2
    ):
        raise SandboxError("invalid_request")
    if not isinstance(sql, str) or not sql.strip():
        return _invalid()
    try:
        if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
            return _invalid("sql_too_large")
    except UnicodeError:
        return _invalid()
    if not isinstance(dataset_id, str) or not dataset_id or len(dataset_id) > 128:
        return _invalid("unknown_dataset")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_ROWS:
        return _invalid("invalid_pagination")
    try:
        payload = json.dumps(
            {"sql": sql, "dataset_id": dataset_id, "max_rows": max_rows},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except UnicodeError:
        return _invalid()
    if len(payload) > MAX_INPUT_BYTES:
        return _invalid("sql_too_large")
    environment = _environment()
    executable = sys.executable
    if os.name == "nt":
        # Bypass the Windows venv redirector's second process while preserving
        # the trusted venv prefix/site-packages in isolated interpreter mode.
        executable = sys._base_executable
        environment["__PYVENV_LAUNCHER__"] = sys.executable
    args = [executable, "-I", "-B", "-m", "outage_explorer.query.sql_compile_worker"]
    if os.name != "nt":
        args = [sys.executable, "-I", "-S", str(ASSETS / "posix_launcher.py"), *args]
    job = None
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
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
            output, status = _capture(process, payload, remaining, MAX_OUTPUT_BYTES)
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
    if status:
        raise SandboxError("resource_limit")
    try:
        return _message(json.loads(output), max_rows=max_rows)
    except (UnicodeError, ValueError, TypeError, KeyError):
        raise SandboxError("sandbox_unavailable") from None
