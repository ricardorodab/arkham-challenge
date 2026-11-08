"""Execute one bounded structured native browse request in a disposable child.

The parent supplies an authorized immutable file and the child independently
checks its identity/schema, recompiles the canonical plan and locks DuckDB's
external-access configuration. No raw SQL field is accepted. This trusted worker
is not the WASM capability sandbox for arbitrary user SQL.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

MAX_INPUT_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_SAFE_INTEGER = (1 << 53) - 1


def _scalar(value):
    """Keep exact decimals/dates/large integers and JSON-native booleans/scalars."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if type(value) is int:
        return value if abs(value) <= MAX_SAFE_INTEGER else str(value)
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _path_matches(path, expected):
    actual = _identity(path.stat(follow_symlinks=False))
    return actual[:4] == expected[:4] if os.name == "nt" else actual == expected


def execute(payload):
    """Validate the protocol, restrict native capabilities, and return capped typed rows."""
    from outage_explorer.analytics.model_contracts import DATASETS
    from outage_explorer.query.browse import compile_browse

    required = {
        "path",
        "identity",
        "fd",
        "dataset_id",
        "plan",
        "limit",
        "last",
        "max_output_bytes",
        "timeout_ms",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        return {"error": "query_failed"}
    dataset = payload["dataset_id"]
    if not isinstance(dataset, str) or dataset not in DATASETS:
        return {"error": "query_failed"}
    maximum, timeout_ms = payload["max_output_bytes"], payload["timeout_ms"]
    if (
        type(maximum) is not int
        or not 256 <= maximum <= MAX_OUTPUT_BYTES
        or type(timeout_ms) is not int
        or not 1 <= timeout_ms <= 8000
    ):
        return {"error": "query_failed"}
    sql, parameters, _ = compile_browse(dataset, payload["plan"], payload["limit"], payload["last"])
    path = Path(payload["path"])
    expected = payload["identity"]
    if not path.is_absolute() or path.is_symlink() or not _path_matches(path, expected):
        return {"error": "data_integrity"}
    fd = payload["fd"]
    if os.name != "nt":
        if type(fd) is not int or fd < 3 or _identity(os.fstat(fd)) != expected:
            return {"error": "data_integrity"}
        read_path = f"/proc/self/fd/{fd}" if sys.platform == "linux" else f"/dev/fd/{fd}"
    else:
        if fd is not None:
            return {"error": "data_integrity"}
        read_path = str(path)
    import duckdb

    if duckdb.__version__ != "2.0.0":
        return {"error": "runtime_unavailable"}
    connection = duckdb.connect(config={"threads": 1, "memory_limit": "256MB"})
    try:
        settings = (
            ("temp_directory", ""),
            ("max_temp_directory_size", "0B"),
            ("autoload_known_extensions", False),
            ("autoinstall_known_extensions", False),
            ("allow_community_extensions", False),
            ("allow_unsigned_extensions", False),
            ("allow_persistent_secrets", False),
            ("allowed_directories", []),
            ("allowed_paths", [read_path]),
            ("max_execution_time", timeout_ms),
            ("enable_external_access", False),
            ("lock_configuration", True),
        )
        for name, value in settings:
            connection.execute("SET " + name + " = ?", [value])
        connection.read_parquet(read_path, hive_partitioning=False).create_view(dataset)
        actual = [
            (item[0], str(item[1]))
            for item in connection.execute(f'DESCRIBE "{dataset}"').fetchall()
        ]
        if actual != list(DATASETS[dataset].columns):
            return {"error": "data_integrity"}
        cursor = connection.execute(sql, parameters)
        columns = [{"name": name, "type": kind} for name, kind in DATASETS[dataset].columns]
        rows = []
        encoded_bytes = len(json.dumps(columns).encode()) + 64
        while row := cursor.fetchone():
            result = [_scalar(value) for value in row]
            encoded_bytes += (
                len(
                    json.dumps(
                        result, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    ).encode()
                )
                + 1
            )
            if encoded_bytes > maximum or len(rows) >= payload["limit"] + 1:
                return {"error": "result_too_large"}
            rows.append(result)
        if not _path_matches(path, expected) or (
            os.name != "nt" and _identity(os.fstat(fd)) != expected
        ):
            return {"error": "data_integrity"}
        return {"columns": columns, "rows": rows}
    except duckdb.OutOfMemoryException:
        return {"error": "resource_limit"}
    except duckdb.InterruptException:
        return {"error": "query_timeout"}
    finally:
        connection.close()


def main() -> None:
    """Read one bounded JSON request and emit only a bounded static-error/result frame."""
    response = {"error": "query_failed"}
    maximum = MAX_OUTPUT_BYTES
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) <= MAX_INPUT_BYTES:
            payload = json.loads(raw)
            if isinstance(payload, dict) and type(payload.get("max_output_bytes")) is int:
                maximum = max(256, min(MAX_OUTPUT_BYTES, payload["max_output_bytes"]))
            response = execute(payload)
    except MemoryError:
        response = {"error": "resource_limit"}
    except ImportError:
        response = {"error": "runtime_unavailable"}
    except Exception:
        pass
    encoded = json.dumps(
        response, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    if len(encoded) > maximum:
        encoded = b'{"error":"result_too_large"}'
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
