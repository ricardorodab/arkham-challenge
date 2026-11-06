"""Real WASM execution probes, independent of the API's SQL AST policy."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import duckdb
import pytest

from outage_explorer import query_sandbox as sandbox

pytestmark = pytest.mark.skipif(
    not sandbox.is_available(), reason="Node 24.13+ and npm ci are required for WASM execution"
)


@pytest.fixture
def parquet(tmp_path):
    path = tmp_path / "selected.parquet"
    con = duckdb.connect()
    try:
        con.execute("""
            CREATE TABLE sample AS SELECT DATE '2026-09-03' AS period,
                'literal <script>alert(1)</script>' AS label,
                97620.5::DECIMAL(24,9) AS capacity_mw,
                (-0.004079369)::DECIMAL(24,9) AS gap_pp,
                0.015440793686::DECIMAL(24,12) AS offline_share,
                TRUE AS observed, NULL::DECIMAL(24,9) AS undefined_metric
        """)
        con.execute("COPY sample TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(path)])
    finally:
        con.close()
    return path


def execute(parquet: Path, sql: str, **kwargs):
    return sandbox.run_query(
        parquet,
        "sample",
        sql,
        expected_sha256=hashlib.sha256(parquet.read_bytes()).hexdigest(),
        **kwargs,
    )


def test_real_parquet_preserves_date_decimal_sign_null_boolean_and_text(parquet):
    result = execute(parquet, "SELECT * FROM sample")
    assert result["rows"] == [
        [
            "2026-09-03",
            "literal <script>alert(1)</script>",
            "97620.500000000",
            "-0.004079369",
            "0.015440793686",
            True,
            None,
        ]
    ]
    assert result["columns"][2] == {"name": "capacity_mw", "type": "DECIMAL(24,9)"}
    assert result["engine"] == "DuckDB-WASM v1.5.4"


def test_aggregate_bigint_and_large_decimal_are_lossless(parquet):
    result = execute(
        parquet,
        """SELECT count(*) AS n,
        99999999999999999999999999999.123456789::DECIMAL(38,9) AS large
        FROM sample""",
    )
    assert result["rows"] == [["1", "99999999999999999999999999999.123456789"]]
    assert result["columns"][0]["type"] == "BIGINT"


@pytest.mark.parametrize("operation", ["read_csv", "read_parquet", "ATTACH", "COPY"])
def test_host_files_and_other_dataset_are_unreachable_without_ast(parquet, tmp_path, operation):
    canary = tmp_path / "private-canary.csv"
    secret = "NEVER_DISCLOSE_HOST_CANARY_481623"
    canary.write_text("secret\n" + secret + "\n")
    target = str(canary).replace("'", "''")
    statements = {
        "read_csv": f"SELECT * FROM read_csv('{target}')",
        "read_parquet": f"SELECT * FROM read_parquet('{target}')",
        "ATTACH": f"ATTACH '{target}' AS forbidden",
        "COPY": f"COPY (SELECT 'overwritten') TO '{target}'",
    }
    with pytest.raises(sandbox.SandboxError) as error:
        execute(parquet, statements[operation])
    assert secret not in str(error.value)
    assert str(canary) not in str(error.value)
    assert canary.read_text() == "secret\n" + secret + "\n"


def test_http_reader_cannot_contact_even_localhost(parquet):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"secret\nremote\n")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/canary.csv"
        with pytest.raises(sandbox.SandboxError):
            execute(parquet, f"SELECT * FROM read_csv('{url}')")
        assert requests == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_runtime_configuration_is_locked(parquet):
    with pytest.raises(sandbox.SandboxError):
        execute(parquet, "SET enable_external_access=true")
    result = execute(parquet, "SELECT current_setting('enable_external_access') AS external")
    assert result["rows"] == [[False]]


def test_row_cap_is_enforced_inside_streaming_worker(parquet):
    result = execute(parquet, "SELECT i FROM range(10000000) t(i)", max_rows=7)
    assert result["rows"] == [[str(i)] for i in range(7)]


def test_response_bytes_are_bounded_before_delivery(parquet):
    with pytest.raises(sandbox.SandboxError, match="response size") as error:
        execute(parquet, "SELECT repeat('x', 3000000)")
    assert error.value.code == "result_too_large"


def test_memory_exhaustion_is_contained_and_next_query_works(parquet):
    with pytest.raises(sandbox.SandboxError) as error:
        execute(parquet, "SELECT repeat('x', 1000000000)")
    assert error.value.code in {"resource_limit", "query_failed"}
    assert execute(parquet, "SELECT count(*) FROM sample")["rows"] == [["1"]]


def test_expensive_running_query_is_terminated_by_wall_deadline(parquet):
    started = time.monotonic()
    with pytest.raises(sandbox.SandboxError) as error:
        execute(
            parquet,
            "SELECT sum(i::HUGEINT) FROM range(1000000000000) t(i)",
            timeout_seconds=3,
        )
    assert error.value.code == "query_timeout"
    assert time.monotonic() - started < 5


def test_timeout_reaps_actual_child_and_does_not_poison_next_query(parquet, monkeypatch):
    real_popen = subprocess.Popen
    processes = []

    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(sandbox.subprocess, "Popen", launch)
    started = time.monotonic()
    with pytest.raises(sandbox.SandboxError) as error:
        execute(parquet, "SELECT count(*) FROM sample", timeout_seconds=0.05)
    assert error.value.code == "query_timeout"
    assert time.monotonic() - started < 2
    assert processes and all(process.poll() is not None for process in processes)
    assert execute(parquet, "SELECT count(*) FROM sample")["rows"] == [["1"]]


def test_actual_process_gets_no_application_secret_or_node_options(parquet, monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "TEST_EIA_ENV_CANARY")
    monkeypatch.setenv("DJANGO_SECRET_KEY", "TEST_DJANGO_ENV_CANARY")
    monkeypatch.setenv("NODE_OPTIONS", "--require nonexistent-secret-loader.cjs")
    real_popen = subprocess.Popen
    received = []

    def launch(*args, **kwargs):
        received.append(kwargs["env"])
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(sandbox.subprocess, "Popen", launch)
    execute(parquet, "SELECT count(*) FROM sample")
    assert received
    assert all(
        not ({"EIA_API_KEY", "DJANGO_SECRET_KEY", "NODE_OPTIONS"} & env.keys()) for env in received
    )


def test_source_checksum_rejection_occurs_before_spawning(parquet, monkeypatch):
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **k: pytest.fail("child spawned"))
    with pytest.raises(sandbox.SandboxError) as error:
        sandbox.run_query(parquet, "sample", "SELECT * FROM sample", expected_sha256="0" * 64)
    assert error.value.code == "source_changed"


def test_nonfinite_results_have_explicit_error(parquet):
    with pytest.raises(sandbox.SandboxError) as error:
        execute(parquet, "SELECT 'NaN'::DOUBLE")
    assert error.value.code == "non_finite_result"


def test_runtime_status_contains_no_local_paths():
    status = sandbox.runtime_status()
    assert status["available"] is True
    assert status["isolation"] == "wasm-buffer-v1"
    assert "D:\\" not in json.dumps(status)
    assert status["process_memory_limit_mib"] == (512 if os.name == "nt" else None)
    assert status["data_allocation_limit_mib"] == (
        (1024 if sys.platform == "linux" else 512) if os.name != "nt" else None
    )


@pytest.mark.parametrize("version", ["v22.13.0", "v24.12.0", "v25.0.0"])
def test_unsupported_node_is_denied_before_reading_selected_data_or_worker_spawn(
    parquet, monkeypatch, version
):
    source_sha256 = hashlib.sha256(parquet.read_bytes()).hexdigest()
    original_open = Path.open

    def protected_open(path, *args, **kwargs):
        if path == parquet:
            pytest.fail("Do not read selected data with an unsupported runtime")
        return original_open(path, *args, **kwargs)

    def worker_forbidden(*args, **kwargs):
        pytest.fail("Do not spawn a worker with an unsupported runtime")

    def version_result(args, **kwargs):
        assert args[-1] == "--version"
        return subprocess.CompletedProcess(args, 0, stdout=version.encode() + b"\n", stderr=b"")

    monkeypatch.setattr(Path, "open", protected_open)
    monkeypatch.setattr(sandbox.subprocess, "run", version_result)
    monkeypatch.setattr(sandbox.subprocess, "Popen", worker_forbidden)
    sandbox._node_version.cache_clear()
    try:
        with pytest.raises(sandbox.SandboxError) as error:
            sandbox.run_query(
                parquet, "sample", "SELECT * FROM sample", expected_sha256=source_sha256
            )
        assert error.value.code == "sandbox_unavailable"
    finally:
        sandbox._node_version.cache_clear()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object boundary")
def test_windows_job_hard_memory_ceiling_blocks_allocation():
    script = (
        "import sys;sys.stdin.buffer.read(1)\n"
        "try: bytearray(700*1024*1024)\n"
        "except MemoryError: sys.exit(77)\n"
    )
    process = subprocess.Popen(
        [sys._base_executable, "-I", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    job = sandbox._WindowsJob(process)
    try:
        process.communicate(b"x", timeout=8)
        assert process.returncode == 77
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        job.close()
