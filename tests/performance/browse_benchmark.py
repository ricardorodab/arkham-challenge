"""Measure the real bounded browse path over a large, wholly synthetic Parquet file.

Run manually with --directory outside the repository. A cold result means the
process integrity cache is empty; this script does not evict OS filesystem caches.
Generation and EXPLAIN are trusted benchmark setup, not public query operations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

from outage_explorer.query import browse_runtime as runtime
from outage_explorer.query.browse import compile_browse, normalize_query

DATASET = "generator_daily"


def _sample(process, metrics):
    """Collect only the benchmark child's peak RSS/CPU and Linux AS readback."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "peak",
                    "rss",
                    "peak_paged",
                    "paged",
                    "peak_nonpaged",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.K32GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
        kernel.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            *([ctypes.POINTER(wintypes.FILETIME)] * 4),
        )
        handle = wintypes.HANDLE(int(process._handle))
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if kernel.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            metrics["peak_rss_bytes_observed"] = max(
                metrics.get("peak_rss_bytes_observed", 0), counters.peak
            )
        stamps = [wintypes.FILETIME() for _ in range(4)]
        if kernel.GetProcessTimes(handle, *(ctypes.byref(stamp) for stamp in stamps)):
            cpu = (
                sum((stamp.dwHighDateTime << 32) | stamp.dwLowDateTime for stamp in stamps[2:])
                / 10_000_000
            )
            metrics["child_cpu_seconds_observed"] = max(
                metrics.get("child_cpu_seconds_observed", 0), cpu
            )
    else:
        status = Path(f"/proc/{process.pid}/status").read_text()
        for line in status.splitlines():
            if line.startswith("VmHWM:"):
                metrics["peak_rss_bytes_observed"] = max(
                    metrics.get("peak_rss_bytes_observed", 0), int(line.split()[1]) * 1024
                )
            if line.startswith("VmPeak:"):
                metrics["peak_virtual_bytes_observed"] = max(
                    metrics.get("peak_virtual_bytes_observed", 0), int(line.split()[1]) * 1024
                )
        fields = Path(f"/proc/{process.pid}/stat").read_text().rsplit(") ", 1)[1].split()
        cpu = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
        metrics["child_cpu_seconds_observed"] = max(
            metrics.get("child_cpu_seconds_observed", 0), cpu
        )
        for line in Path(f"/proc/{process.pid}/limits").read_text().splitlines():
            if line.startswith("Max address space") and line.split()[-2].isdigit():
                metrics["address_space_hard_limit_bytes"] = int(line.split()[-2])


def _operation(path, digest, name, parameters, *, last=None):
    metrics = {}
    original = runtime._capture

    def capture(process, *args):
        stopped = threading.Event()

        def observe():
            while not stopped.is_set():
                try:
                    _sample(process, metrics)
                except (OSError, ValueError, IndexError):
                    pass
                stopped.wait(0.005)

        observer = threading.Thread(target=observe)
        observer.start()
        try:
            return original(process, *args)
        finally:
            try:
                _sample(process, metrics)
            except (OSError, ValueError, IndexError):
                pass
            stopped.set()
            observer.join(timeout=1)

    runtime._capture = capture
    started, cpu_started = time.perf_counter(), time.process_time()
    result = None
    try:
        result = runtime.run_browse(
            path,
            DATASET,
            normalize_query(DATASET, parameters),
            limit=100,
            last=last,
            expected_sha256=digest,
        )
        outcome = {
            "status": "ok",
            "rows_including_sentinel": len(result["rows"]),
            "response_bytes": len(
                json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
            ),
        }
    except runtime.BrowseRuntimeError as error:
        outcome = {"status": error.code}
    finally:
        runtime._capture = original
    return {
        "name": name,
        **outcome,
        "wall_seconds": round(time.perf_counter() - started, 6),
        "parent_cpu_seconds": round(time.process_time() - cpu_started, 6),
        **metrics,
    }, result


def _generate(path, rows):
    """Write high-entropy, valid-schema rows whose compressed file exceeds 64 MiB."""
    with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as connection:
        connection.execute("SET temp_directory = ?", [""])
        connection.execute("SET max_temp_directory_size = ?", ["0B"])
        connection.execute(
            """
            COPY (
                SELECT DATE '2020-01-01' + CAST(i // 10000 AS INTEGER) AS period,
                    lpad(CAST((i // 10) % 1000 AS VARCHAR), 6, '0') AS facility_id,
                    lpad(CAST(i % 10 AS VARCHAR), 3, '0') AS generator_id,
                    sha256('facility-' || CAST(i % 1000 AS VARCHAR)) AS facility_name,
                    CAST(1000 + (i % 1000) / 1000 AS DECIMAL(24,9)) AS capacity_mw,
                    CAST((i % 500) / 10 AS DECIMAL(24,9)) AS outage_mw,
                    CAST((i % 100) / 100 AS DECIMAL(24,9)) AS reported_outage_pct,
                    '[]'::VARCHAR AS quality_flags,
                    sha256(CAST(i AS VARCHAR)) AS source_row_hash
                FROM range(?) generated(i)
            ) TO ? (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)
        """,
            [rows, str(path)],
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=3_000_000)
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    root = (
        Path(__file__).resolve().parents[2]
        if "__file__" in globals() and Path(__file__).is_file()
        else None
    )
    directory = arguments.directory.resolve()
    if root is not None and (directory == root or root in directory.parents):
        raise SystemExit("Choose a benchmark directory outside the repository.")
    if arguments.rows < 2_000_000:
        raise SystemExit("The scale benchmark requires at least two million synthetic rows.")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (DATASET + ".parquet")
    if not arguments.reuse:
        if path.exists():
            raise SystemExit("Synthetic destination already exists; use --reuse explicitly.")
        _generate(path, arguments.rows)
    if path.stat().st_size <= 64 * 1024 * 1024:
        raise SystemExit("Synthetic Parquet must genuinely exceed 64 MiB.")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    explanations = {}
    with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as connection:
        connection.read_parquet(str(path)).create_view(DATASET)
        actual_rows = connection.execute(f'SELECT count(*) FROM "{DATASET}"').fetchone()[0]
        if actual_rows != arguments.rows:
            raise SystemExit("Synthetic row count does not match the requested benchmark.")
        for name, parameters in (("default", {}), ("column_sort", {"sort": "facility_name"})):
            sql, values, _ = compile_browse(DATASET, normalize_query(DATASET, parameters), 100)
            text = "\n".join(
                str(row[-1]) for row in connection.execute("EXPLAIN " + sql, values).fetchall()
            )
            explanations[name] = {
                "top_n": "TOP_N" in text.upper().replace(" ", "_"),
                "plan": text.replace(str(path), "<synthetic-parquet>"),
            }
    with runtime._CACHE_LOCK:
        runtime._VERIFIED.clear()
    operations = []
    result, first = _operation(path, digest, "cold_integrity_first_page", {})
    operations.append(result)
    result, _ = _operation(path, digest, "warm_first_page", {})
    operations.append(result)
    if first and len(first["rows"]) >= 100:
        result, _ = _operation(
            path, digest, "warm_second_seek_page", {}, last=first["rows"][99][:3]
        )
        operations.append(result)
    index = min(2_000_000, arguments.rows - 1) - 1
    deep = [
        (date(2020, 1, 1) + timedelta(days=index // 10000)).isoformat(),
        f"{(index // 10) % 1000:06}",
        f"{index % 10:03}",
    ]
    result, _ = _operation(path, digest, "warm_deep_seek_page", {}, last=deep)
    operations.append(result)
    for name, parameters in (
        ("warm_column_sort", {"sort": "facility_name"}),
        ("warm_global_contains_search", {"search": "not-present-synthetic"}),
    ):
        result, _ = _operation(path, digest, name, parameters)
        operations.append(result)
    report = {
        "synthetic": True,
        "dataset": DATASET,
        "rows": actual_rows,
        "file_bytes": path.stat().st_size,
        "file_sha256": digest,
        "platform": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "cache_scope": "Cold integrity cache only; OS filesystem caches were not evicted.",
        "memory_scope": "Child peak RSS is observed at 5ms intervals; process hard bound remains separate.",
        "limits": {
            "engine_memory": "256MB",
            "process_memory_bytes_windows_or_address_space_posix": 536870912,
            "cpu_seconds": 6,
            "wall_seconds": 8,
            "response_bytes": 2097152,
            "maximum_rows": 1001,
        },
        "explain": explanations,
        "operations": operations,
        "limitations": "Keyset avoids OFFSET growth; filters/global contains may still scan. No O(log n) or browser full-materialization claim.",
    }
    if arguments.output:
        arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
