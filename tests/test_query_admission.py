"""Real shared locks and one execution budget across compilation and WASM."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import ExitStack
from contextvars import Context
from types import SimpleNamespace

import pytest

from outage_explorer.data.storage import writer_lock
from outage_explorer.query.query_sandbox import _environment
from outage_explorer.query.sql_compiler import Compilation
from outage_explorer.query.sql_policy import ValidatedQuery
from outage_explorer.service import admission, api_services
from outage_explorer.service.errors import APIError


@pytest.fixture
def directory(settings, tmp_path):
    settings.OUTAGE_STATE_DIR = tmp_path / "admission-state"
    settings.OUTAGE_QUERY_TIMEOUT_SECONDS = 8
    return settings.OUTAGE_STATE_DIR / "query-admission"


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=100.0)

    def advance(seconds):
        value.now += seconds

    monkeypatch.setattr(
        admission, "time", SimpleNamespace(monotonic=lambda: value.now, sleep=advance)
    )
    return value


def test_nested_same_user_reuses_slot_without_resetting_deadline(directory, clock):
    with admission.query_slot(10):
        assert admission.remaining_query_seconds() == 8
        clock.now += 1.5
        with admission.query_slot(10):
            assert admission.remaining_query_seconds() == 6.5
            assert admission.remaining_query_seconds(2) == 2
        assert admission.remaining_query_seconds() == 6.5
    assert admission._ACTIVE.get() is None


def test_nested_different_user_cannot_borrow_an_active_slot(directory):
    with admission.query_slot(10), pytest.raises(APIError) as error, admission.query_slot(11):
        pytest.fail("A different user must not borrow active execution authority")
    assert error.value.status == 429 and error.value.code == "query_capacity"


def test_one_running_query_per_user_in_independent_request_context(directory):
    def competing_request():
        with pytest.raises(APIError) as error, admission.query_slot(10):
            pytest.fail("Competing request entered the same user's active query")
        assert error.value.code == "user_query_busy"

    with admission.query_slot(10):
        Context().run(competing_request)
    with admission.query_slot(10):
        pass


def test_two_global_slots_and_five_second_capacity_wait(directory, clock):
    def competing_request():
        started = clock.now
        with pytest.raises(APIError) as error, admission.query_slot(12):
            pytest.fail("A third global execution was admitted")
        assert error.value.code == "query_capacity"
        assert 5 <= clock.now - started < 5.1

    with writer_lock(directory / "slot-0"), admission.query_slot(11):
        Context().run(competing_request)
    with admission.query_slot(12):
        pass


def test_eight_waiting_positions_bound_queue_and_release_user_on_rejection(directory):
    with ExitStack() as held:
        for index in range(8):
            held.enter_context(writer_lock(directory / f"queue-{index}"))
        with pytest.raises(APIError) as error, admission.query_slot(20):
            pytest.fail("A ninth waiting query was admitted")
        assert error.value.code == "query_queue_full"
        assert error.value.retry_after == 1
    with admission.query_slot(20):
        pass


def test_execution_exception_releases_all_locks_and_context(directory):
    with pytest.raises(RuntimeError), admission.query_slot(30):
        raise RuntimeError("synthetic execution failure")
    assert admission._ACTIVE.get() is None
    with admission.query_slot(30):
        pass


def test_per_user_admission_is_shared_with_an_actual_other_process(directory, settings):
    script = """
import json
from outage_explorer.service.admission import query_slot
from outage_explorer.service.errors import APIError
try:
    with query_slot(40):
        print(json.dumps({'admitted':True}))
except APIError as error:
    print(json.dumps({'admitted':False,'code':error.code}))
"""
    environment = _environment()
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "outage_explorer.web.settings",
            "OUTAGE_STATE_DIR": str(settings.OUTAGE_STATE_DIR),
            "OUTAGE_DJANGO_SECRET_KEY": "synthetic-admission-process-key-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        }
    )
    executable = sys.executable
    if os.name == "nt":
        executable = sys._base_executable
        environment["__PYVENV_LAUNCHER__"] = sys.executable

    def child():
        result = subprocess.run(
            [executable, "-I", "-B", "-c", script],
            env=environment,
            capture_output=True,
            check=True,
            timeout=5,
        )
        return json.loads(result.stdout)

    with admission.query_slot(40):
        assert child() == {"admitted": False, "code": "user_query_busy"}
    assert child() == {"admitted": True}


@pytest.mark.django_db
@pytest.mark.parametrize("elapsed,expected_status", [(7.5, 200), (8.1, 504)])
def test_compiler_and_engine_share_one_budget_without_starting_expired_work(
    authenticated_client, monkeypatch, clock, elapsed, expected_status
):
    client, _ = authenticated_client("viewer")
    worker_deadlines = []

    def compile_query(sql, dataset_id, *, max_rows, timeout_seconds):
        assert admission._ACTIVE.get() is not None
        assert timeout_seconds == 2
        clock.now += elapsed
        return Compilation(
            (dataset_id,),
            ValidatedQuery("SELECT period FROM fleet_daily", (dataset_id,), max_rows),
            None,
        )

    def execute(*args, **kwargs):
        worker_deadlines.append(kwargs["timeout_seconds"])
        return {"columns": [{"name": "period", "type": "DATE"}], "rows": [["2026-09-01"]]}

    monkeypatch.setattr(api_services, "compile_sql", compile_query)
    monkeypatch.setattr(api_services, "run_query", execute)
    result = client.post(
        "/api/v1/query",
        json.dumps({"dataset_id": "fleet_daily", "sql": "SELECT period FROM fleet_daily"}),
        content_type="application/json",
    )
    assert result.status_code == expected_status, result.content
    if expected_status == 200:
        assert worker_deadlines == [0.5]
    else:
        assert result.json()["code"] == "query_timeout"
        assert worker_deadlines == []
    assert admission._ACTIVE.get() is None
