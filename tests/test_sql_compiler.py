"""Real isolated compilation, denial/reference ordering and supervisor bounds."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from outage_explorer import sql_compiler, sql_policy
from outage_explorer.query_sandbox import SandboxError


def test_select_compiles_in_child_without_calling_parent_parser(monkeypatch):
    def no_parent_parse(*args, **kwargs):
        pytest.fail("Submitted SQL must never be parsed in the web process")

    monkeypatch.setattr(sql_policy, "referenced_dataset_ids", no_parent_parse)
    monkeypatch.setattr(sql_policy, "validate_sql", no_parent_parse)
    compiled = sql_compiler.compile_sql(
        "SELECT period,offline_pct FROM fleet_daily WHERE offline_pct > 1",
        "fleet_daily",
        max_rows=23,
    )
    assert compiled.dataset_ids == ("fleet_daily",)
    assert compiled.error is None
    assert compiled.query.row_limit == 23
    assert '"fleet_daily"."period" ASC' in compiled.query.sql
    assert "LIMIT 24" in compiled.query.sql


@pytest.mark.parametrize(
    "sql,references",
    [
        (
            "WITH private AS(SELECT * FROM generator_daily) SELECT * FROM fleet_daily",
            ("fleet_daily", "generator_daily"),
        ),
        (
            "SELECT * FROM fleet_daily WHERE EXISTS(SELECT 1 FROM facility_daily)",
            ("facility_daily", "fleet_daily"),
        ),
        (
            "SELECT * FROM fleet_daily JOIN generator_daily USING(period)",
            ("fleet_daily", "generator_daily"),
        ),
    ],
)
def test_forbidden_shapes_preserve_all_references_for_prior_authorization(sql, references):
    compiled = sql_compiler.compile_sql(sql, "fleet_daily")
    assert compiled.dataset_ids == references
    assert compiled.query is None
    assert compiled.error.code == "dataset_mismatch"


@pytest.mark.parametrize(
    "sql,code",
    [
        ("DELETE FROM fleet_daily", "unsupported_sql"),
        ("COPY fleet_daily TO 'private.csv'", "unsupported_sql"),
        ("SELECT * FROM fleet_daily; DELETE FROM fleet_daily", "multiple_statements"),
        ("SELECT " + "(" * 40 + "1" + ")" * 40 + " FROM fleet_daily", "sql_too_complex"),
    ],
)
def test_writes_multiple_commands_and_complexity_fail_without_diagnostics(sql, code):
    compiled = sql_compiler.compile_sql(sql, "fleet_daily")
    assert compiled.query is None
    assert compiled.error.code == code
    assert sql not in str(compiled.error)


def test_external_file_reader_is_parsed_but_never_executed(tmp_path, caplog):
    canary = tmp_path / "must-not-read.csv"
    canary.write_text("secret\nNEVER_DISCLOSE_COMPILER_FILE_CANARY\n")
    sql = f"SELECT * FROM read_csv('{str(canary).replace(chr(92), '/')}')"
    compiled = sql_compiler.compile_sql(sql, "fleet_daily")
    assert compiled.query is None
    assert compiled.error.code == "unsupported_sql"
    assert "NEVER_DISCLOSE_COMPILER_FILE_CANARY" not in str(compiled.error) + caplog.text
    assert str(canary) not in str(compiled.error) + caplog.text


def test_input_limit_is_checked_without_spawning(monkeypatch):
    monkeypatch.setattr(
        sql_compiler.subprocess, "Popen", lambda *a, **k: pytest.fail("Do not spawn oversized SQL")
    )
    compiled = sql_compiler.compile_sql("SELECT " + "x" * 16384, "fleet_daily")
    assert compiled.query is None and compiled.error.code == "sql_too_large"


def test_invalid_unicode_does_not_enter_child(monkeypatch):
    monkeypatch.setattr(
        sql_compiler.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("Do not spawn invalid Unicode"),
    )
    compiled = sql_compiler.compile_sql("SELECT '\ud800' FROM fleet_daily", "fleet_daily")
    assert compiled.query is None and compiled.error.code == "invalid_sql"


def test_compiler_payload_and_environment_have_no_application_authority(monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "SYNTHETIC_EIA_COMPILER_CANARY")
    monkeypatch.setenv("OUTAGE_DJANGO_SECRET_KEY", "SYNTHETIC_DJANGO_COMPILER_CANARY")
    monkeypatch.setenv("OUTAGE_DATA_DIR", "D:/private/analytical-data")
    monkeypatch.setenv("OUTAGE_STATE_DIR", "D:/private/sessions")
    monkeypatch.setenv("PYTHONPATH", "D:/private/untrusted-python")
    real_popen = subprocess.Popen
    real_capture = sql_compiler._capture
    received = []

    def launch(*args, **kwargs):
        assert not (
            {
                "EIA_API_KEY",
                "OUTAGE_DJANGO_SECRET_KEY",
                "OUTAGE_DATA_DIR",
                "OUTAGE_STATE_DIR",
                "PYTHONPATH",
            }
            & kwargs["env"].keys()
        )
        assert "-I" in args[0] and "-B" in args[0]
        received.append(True)
        return real_popen(*args, **kwargs)

    def capture(process, payload, *args):
        assert set(json.loads(payload)) == {"sql", "dataset_id", "max_rows"}
        assert len(payload) <= 32768
        return real_capture(process, payload, *args)

    monkeypatch.setattr(sql_compiler.subprocess, "Popen", launch)
    monkeypatch.setattr(sql_compiler, "_capture", capture)
    assert sql_compiler.compile_sql("SELECT * FROM fleet_daily", "fleet_daily").error is None
    assert received


def test_hung_compiler_is_killed_reaped_and_next_compile_works(monkeypatch):
    real_popen = subprocess.Popen
    processes = []

    def hang(args, **kwargs):
        executable = args[0]
        script = "import sys,time;sys.stdin.buffer.read();time.sleep(60)"
        process = real_popen([executable, "-I", "-B", "-c", script], **kwargs)
        processes.append(process)
        return process

    with monkeypatch.context() as patch:
        patch.setattr(sql_compiler.subprocess, "Popen", hang)
        started = time.monotonic()
        with pytest.raises(SandboxError) as error:
            sql_compiler.compile_sql(
                "SELECT * FROM fleet_daily", "fleet_daily", timeout_seconds=0.1
            )
        assert error.value.code == "query_timeout"
        assert time.monotonic() - started < 1.5
        assert processes and all(process.poll() is not None for process in processes)
    assert sql_compiler.compile_sql("SELECT * FROM fleet_daily", "fleet_daily").error is None


def test_compiler_output_is_bounded_and_failure_is_reaped(monkeypatch):
    real_popen = subprocess.Popen
    processes = []

    def oversized(args, **kwargs):
        script = "import sys;sys.stdin.buffer.read();sys.stdout.write('x'*1000000)"
        process = real_popen([args[0], "-I", "-B", "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(sql_compiler.subprocess, "Popen", oversized)
    with pytest.raises(SandboxError) as error:
        sql_compiler.compile_sql("SELECT * FROM fleet_daily", "fleet_daily")
    assert error.value.code == "result_too_large"
    assert processes and all(process.poll() is not None for process in processes)


def test_unavailable_compiler_never_falls_back_to_parent_parser(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("synthetic private environment details")

    monkeypatch.setattr(sql_compiler.subprocess, "Popen", unavailable)
    monkeypatch.setattr(
        sql_policy, "validate_sql", lambda *a, **k: pytest.fail("No in-process parser fallback")
    )
    with pytest.raises(SandboxError) as error:
        sql_compiler.compile_sql("SELECT * FROM fleet_daily", "fleet_daily")
    assert error.value.code == "sandbox_unavailable"
    assert "synthetic" not in str(error.value)
