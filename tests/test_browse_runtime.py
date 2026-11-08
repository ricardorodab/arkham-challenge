"""Exercise the supervised structured native path with synthetic immutable files."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

import duckdb
import pytest

from outage_explorer.analytics.model_contracts import DATASETS
from outage_explorer.query import browse_runtime as runtime
from outage_explorer.query import browse_worker
from outage_explorer.query.browse import compile_browse, normalize_query


def write_dataset(path, dataset, rows):
    """Write exact declared types without model publication or upstream access."""
    columns = ",".join(f'"{name}" {kind}' for name, kind in DATASETS[dataset].columns)
    with duckdb.connect(config={"threads": 1}) as connection:
        connection.execute("CREATE TABLE selected (" + columns + ")")
        connection.executemany(
            "INSERT INTO selected VALUES ("
            + ",".join("?" for _ in DATASETS[dataset].columns)
            + ")",
            rows,
        )
        connection.execute("COPY selected TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(path)])
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def facilities(tmp_path):
    path = tmp_path / "dim_facility.parquet"
    digest = write_dataset(
        path,
        "dim_facility",
        [
            ("001", "Zulu", date(2026, 1, 1), date(2026, 9, 1), True, False),
            ("002", "Alpha", date(2026, 1, 2), None, False, True),
            ("003", "Alpha", date(2026, 1, 3), None, True, True),
        ],
    )
    return path, digest


def execute(facilities, parameters=None, *, limit=2, last=None, **options):
    path, digest = facilities
    return runtime.run_browse(
        path,
        "dim_facility",
        normalize_query("dim_facility", parameters or {}),
        limit=limit,
        last=last,
        expected_sha256=digest,
        **options,
    )


def test_real_native_child_returns_sentinel_exact_dates_and_booleans(facilities):
    result = execute(facilities)
    assert result["columns"] == [
        {"name": name, "type": kind} for name, kind in DATASETS["dim_facility"].columns
    ]
    assert len(result["rows"]) == 3
    assert result["rows"][0] == ["001", "Zulu", "2026-01-01", "2026-09-01", True, False]
    assert type(result["rows"][0][-1]) is bool


def test_seek_and_bound_filters_execute_in_the_child(facilities):
    parameters = {"sort": "facility_name", "direction": "asc"}
    plan = normalize_query("dim_facility", parameters)
    first = execute(facilities, parameters, limit=1)
    _, _, order = compile_browse("dim_facility", plan, 1)
    names = [entry["name"] for entry in first["columns"]]
    last = [first["rows"][0][names.index(name)] for name in order]
    second = execute(facilities, parameters, limit=1, last=last)
    assert [first["rows"][0][0], second["rows"][0][0]] == ["002", "003"]
    filtered = execute(
        facilities, {"filters": [{"column": "seen_in_generator_route", "op": "eq", "value": True}]}
    )
    assert [row[0] for row in filtered["rows"]] == ["002", "003"]


def test_native_decimal38_does_not_round_through_float(tmp_path):
    dataset = "facility_reconciliation_daily"
    large = Decimal("12345678901234567890123456789.123456789")
    row = []
    for name, kind in DATASETS[dataset].columns:
        row.append(
            date(2026, 9, 1)
            if kind == "DATE"
            else True
            if kind == "BOOLEAN"
            else 7
            if kind == "INTEGER"
            else large
            if kind == "DECIMAL(38,9)"
            else Decimal("1.123456789")
            if kind.startswith("DECIMAL")
            else name
        )
    path = tmp_path / (dataset + ".parquet")
    digest = write_dataset(path, dataset, [row])
    result = runtime.run_browse(
        path, dataset, normalize_query(dataset, {}), limit=1, expected_sha256=digest
    )
    position = [name for name, _ in DATASETS[dataset].columns].index("generator_capacity_mw")
    assert result["rows"][0][position] == str(large)


def test_wrong_hash_and_changed_identity_reject_before_native_launch(facilities, monkeypatch):
    path, digest = facilities
    execute(facilities)  # Prime the stat-bound verification cache.
    original = runtime.subprocess.Popen
    launched = []

    def recorded(*args, **kwargs):
        launched.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.subprocess, "Popen", recorded)
    with pytest.raises(runtime.BrowseRuntimeError, match="data_integrity"):
        runtime.run_browse(
            path,
            "dim_facility",
            normalize_query("dim_facility", {}),
            limit=1,
            expected_sha256="0" * 64,
        )
    with path.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(runtime.BrowseRuntimeError, match="data_integrity"):
        execute((path, digest))
    assert launched == []


def test_raw_sql_protocol_and_noncanonical_plan_are_rejected(facilities):
    path, digest = facilities
    with pytest.raises(runtime.BrowseRuntimeError, match="query_failed"):
        runtime.run_browse(
            path,
            "dim_facility",
            {"sql": "SELECT * FROM read_csv('/etc/passwd')"},
            limit=1,
            expected_sha256=digest,
        )


def test_same_size_overwrite_with_restored_mtime_invalidates_warm_cache(facilities):
    path, _ = facilities
    execute(facilities)
    before = path.stat()
    with path.open("r+b") as stream:
        original = stream.read(1)
        stream.seek(0)
        stream.write(bytes([original[0] ^ 1]))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(runtime.BrowseRuntimeError, match="data_integrity"):
        execute(facilities)


def test_output_and_deadline_failure_recover_on_next_child(facilities):
    with pytest.raises(runtime.BrowseRuntimeError, match="result_too_large"):
        execute(facilities, max_output_bytes=256)
    with pytest.raises(runtime.BrowseRuntimeError, match="query_timeout"):
        execute(facilities, timeout_seconds=0.000001)
    assert execute(facilities)["rows"][0][0] == "001"


def test_integrity_cache_is_bounded_and_avoids_repeat_full_reads(facilities, monkeypatch):
    with runtime._CACHE_LOCK:
        runtime._VERIFIED.clear()
    source_identity = facilities[0].stat()
    original = os.read
    reads = []

    def recorded(descriptor, size):
        # os is shared with subprocess: POSIX startup can also read an error pipe.
        # Observe only the selected Parquet file, not unrelated process-control I/O.
        source_read = os.path.samestat(os.fstat(descriptor), source_identity)
        data = original(descriptor, size)
        if source_read:
            reads.append(len(data))
        return data

    monkeypatch.setattr(runtime.os, "read", recorded)
    execute(facilities)
    assert sum(reads) == source_identity.st_size
    reads.clear()
    execute(facilities)
    assert reads == []
    assert len(runtime._VERIFIED) <= runtime.CACHE_SIZE


def test_parent_does_not_forward_host_credentials_to_native_child(facilities, monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "synthetic-native-key")
    monkeypatch.setenv("OUTAGE_DJANGO_SECRET_KEY", "synthetic-django-key")
    original = runtime.subprocess.Popen
    observed = []

    def checked(*args, **kwargs):
        observed.append(dict(kwargs["env"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.subprocess, "Popen", checked)
    execute(facilities)
    assert len(observed) == 1
    assert "EIA_API_KEY" not in observed[0] and "OUTAGE_DJANGO_SECRET_KEY" not in observed[0]
    assert "HOME" not in observed[0] and "PATH" not in observed[0]


def test_integrity_lru_really_evicts_old_stat_identities(tmp_path):
    with runtime._CACHE_LOCK:
        runtime._VERIFIED.clear()
    for index in range(runtime.CACHE_SIZE + 1):
        path = tmp_path / f"synthetic-{index}.parquet"
        data = f"synthetic integrity bytes {index}".encode()
        path.write_bytes(data)
        descriptor = runtime._open_source(path)
        try:
            runtime._verify_source(
                path, descriptor, hashlib.sha256(data).hexdigest(), time.monotonic() + 8
            )
        finally:
            os.close(descriptor)
    assert len(runtime._VERIFIED) == runtime.CACHE_SIZE
    assert not any(key[0].endswith("synthetic-0.parquet") for key in runtime._VERIFIED)


def test_linked_path_and_parent_directory_are_rejected(facilities, tmp_path):
    path, digest = facilities
    link = tmp_path / "linked.parquet"
    try:
        link.symlink_to(path)
        directory_link = tmp_path / "directory-link"
        directory_link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Creating synthetic symlinks requires platform permission.")
    for selected in (link, directory_link / path.name):
        with pytest.raises(runtime.BrowseRuntimeError, match="data_integrity"):
            runtime.run_browse(
                selected,
                "dim_facility",
                normalize_query("dim_facility", {}),
                limit=1,
                expected_sha256=digest,
            )


def _engine_payload(facilities, descriptor):
    path, _ = facilities
    return {
        "path": str(path),
        "identity": list(runtime._descriptor_identity(descriptor)),
        "fd": descriptor if os.name != "nt" else None,
        "dataset_id": "dim_facility",
        "plan": normalize_query("dim_facility", {}),
        "limit": 2,
        "last": None,
        "max_output_bytes": 2 * 1024 * 1024,
        "timeout_ms": 8000,
    }


@pytest.mark.parametrize("attempt", ["read_other", "write_other", "unlock", "enable_external"])
def test_engine_lockdown_denies_unapproved_io_and_configuration(
    facilities, tmp_path, monkeypatch, attempt
):
    """Bypass only the trusted compiler in this test to probe DuckDB's own controls."""
    canary = tmp_path / "unapproved-synthetic.txt"
    canary.write_text("synthetic canary bytes", encoding="utf-8")
    output = tmp_path / "unapproved-output.csv"
    sql, parameters = {
        "read_other": ("SELECT * FROM read_blob(?)", [str(canary)]),
        "write_other": ("COPY (SELECT 1) TO ? (FORMAT CSV)", [str(output)]),
        "unlock": ("SET lock_configuration = false", []),
        "enable_external": ("SET enable_external_access = true", []),
    }[attempt]
    monkeypatch.setattr(
        "outage_explorer.query.browse.compile_browse", lambda *args: (sql, parameters, [])
    )
    descriptor = runtime._open_source(facilities[0])
    try:
        with pytest.raises(duckdb.Error):
            browse_worker.execute(_engine_payload(facilities, descriptor))
    finally:
        os.close(descriptor)
    assert canary.read_text(encoding="utf-8") == "synthetic canary bytes"
    assert not output.exists()


def test_engine_url_access_is_denied_without_contacting_loopback_canary(facilities, monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(True)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"synthetic canary")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/synthetic.parquet"
    monkeypatch.setattr(
        "outage_explorer.query.browse.compile_browse",
        lambda *args: ("SELECT * FROM read_parquet(?)", [url], []),
    )
    descriptor = runtime._open_source(facilities[0])
    try:
        with pytest.raises(duckdb.Error):
            browse_worker.execute(_engine_payload(facilities, descriptor))
    finally:
        os.close(descriptor)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert requests == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX native bootstrap resource probe")
def test_native_bootstrap_hard_address_cap_denies_large_allocation_and_recovers():
    code = """import json,resource
denied=False
try:
    value=bytearray(768*1024*1024)
except MemoryError:
    denied=True
print(json.dumps({'denied':denied,'as':resource.getrlimit(resource.RLIMIT_AS),'cpu':resource.getrlimit(resource.RLIMIT_CPU),'fsize':resource.getrlimit(resource.RLIMIT_FSIZE),'fds':resource.getrlimit(resource.RLIMIT_NOFILE)}))"""
    command = [
        sys.executable,
        "-I",
        "-S",
        str(runtime.ASSETS / "browse_launcher.py"),
        sys.executable,
        "-I",
        "-S",
        "-c",
        code,
    ]
    for _ in range(2):
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=8)
        values = json.loads(result.stdout)
        assert values == {
            "denied": True,
            "as": [536870912, 536870912],
            "cpu": [6, 6],
            "fsize": [0, 0],
            "fds": [64, 64],
        }
