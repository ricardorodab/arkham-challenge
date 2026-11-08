from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from outage_explorer import cli
from outage_explorer.data.domain import DataValidationError
from outage_explorer.data.eia import EIAClientError
from outage_explorer.data.pipeline import IngestConfig, ingest
from outage_explorer.data.storage import StorageError, writer_lock

from .helpers import SECRET, FakeClient


@pytest.fixture(autouse=True)
def restore_cli_logging_state():
    logger = logging.getLogger("outage_explorer")
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    yield
    for handler in logger.handlers:
        if handler not in handlers:
            handler.close()
    logger.handlers = handlers
    logger.setLevel(level)
    logger.propagate = propagate


def forbid_client(*args, **kwargs):
    raise AssertionError("This command must not construct an EIA HTTP client")


@pytest.fixture
def published_data(tmp_path: Path):
    manifest = ingest(IngestConfig(tmp_path, mode="full"), FakeClient())
    return tmp_path, manifest


def test_ingest_requires_environment_key_before_network_or_storage(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.setattr("outage_explorer.data.eia.build_opener", forbid_client)
    destination = tmp_path / "not-created"
    assert cli.main(["ingest", "--data-dir", str(destination)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == "configuration"
    assert "EIA_API_KEY" in error["message"]
    assert not destination.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--page-size", "0"],
        ["--mode", "recent", "--start", "2026-09-01"],
        ["--mode", "full", "--start", "2026-09-03", "--end", "2026-09-01"],
    ],
)
def test_invalid_ingest_configuration_returns_two_without_http(
    monkeypatch, tmp_path, capsys, arguments
):
    monkeypatch.setenv("EIA_API_KEY", SECRET)
    monkeypatch.setattr(cli, "EIAClient", forbid_client)
    assert cli.main(["ingest", "--data-dir", str(tmp_path), *arguments]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "configuration"
    assert SECRET not in captured.err


def test_compact_calendar_date_is_rejected_by_cli_parser(monkeypatch, capsys):
    monkeypatch.setattr(cli, "EIAClient", forbid_client)
    with pytest.raises(SystemExit) as caught:
        cli.main(["ingest", "--mode", "full", "--start", "20260901"])
    assert caught.value.code == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["status", "history", "verify"])
def test_readonly_commands_need_no_api_key_or_network(monkeypatch, published_data, capsys, command):
    directory, manifest = published_data
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.setattr(cli, "EIAClient", forbid_client)
    assert cli.main([command, "--data-dir", str(directory)]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == ""
    assert SECRET not in captured.out
    if command == "status":
        assert result["current_snapshot"] == manifest["snapshot_id"]
        assert set(result["routes"]) == {"national", "facility", "generator"}
    elif command == "history":
        assert result["runs"][0]["status"] == "succeeded"
        assert result["runs"][0]["snapshot_id"] == manifest["snapshot_id"]
    else:
        assert result["verified"] is True
        assert result["snapshot_id"] == manifest["snapshot_id"]


def test_status_without_snapshot_is_valid_json_not_a_network_request(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.setattr(cli, "EIAClient", forbid_client)
    assert cli.main(["status", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"current_snapshot": None, "routes": {}}


def test_verify_without_snapshot_returns_documented_operational_error(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.setattr(cli, "EIAClient", forbid_client)
    assert cli.main(["verify", "--data-dir", str(tmp_path)]) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "no_snapshot"


def test_history_rejects_invalid_limits_without_api_key(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.setattr(cli, "EIAClient", forbid_client)
    assert cli.main(["history", "--data-dir", str(tmp_path), "--limit", "0"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "configuration"


def test_successful_ingest_emits_json_and_secret_free_structured_logs(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("EIA_API_KEY", "  " + SECRET + "  ")
    received_keys = []

    def fake_client(key):
        received_keys.append(key)
        return FakeClient()

    monkeypatch.setattr(cli, "EIAClient", fake_client)
    code = cli.main(["ingest", "--data-dir", str(tmp_path), "--mode", "full", "--record-responses"])
    captured = capsys.readouterr()
    assert code == 0
    assert received_keys == [SECRET]
    manifest = json.loads(captured.out)
    assert set(manifest["routes"]) == {"national", "facility", "generator"}
    assert manifest["routes"]["national"]["rows"] == 3
    logs = [json.loads(line) for line in captured.err.splitlines()]
    assert {"run_started", "route_started", "route_completed", "run_succeeded"}.issubset(
        {record["event"] for record in logs}
    )
    assert all({"timestamp", "level", "event"}.issubset(record) for record in logs)
    assert SECRET not in captured.out + captured.err
    for path in tmp_path.rglob("*.json"):
        assert SECRET not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "failure,expected_code,expected_error",
    [
        (EIAClientError("bad_credentials", "Rejected " + SECRET), 3, "bad_credentials"),
        (
            EIAClientError("network_error", "Unreachable " + SECRET, retryable=True),
            3,
            "network_error",
        ),
        (DataValidationError("Bad source value " + SECRET), 4, "invalid_data"),
        (StorageError("Locked " + SECRET, code="already_running"), 5, "already_running"),
        (StorageError("Corrupt " + SECRET, code="corrupt_snapshot"), 4, "corrupt_snapshot"),
        (KeyboardInterrupt(), 130, "interrupted"),
        (RuntimeError("Unexpected implementation detail " + SECRET), 4, "internal_error"),
    ],
)
def test_typed_failures_have_exit_codes_and_safe_json(
    monkeypatch, tmp_path, capsys, failure, expected_code, expected_error
):
    monkeypatch.setenv("EIA_API_KEY", SECRET)
    monkeypatch.setattr(cli, "EIAClient", lambda key: FakeClient())

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(cli, "ingest", fail)
    assert cli.main(["ingest", "--data-dir", str(tmp_path)]) == expected_code
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == expected_error
    assert SECRET not in captured.err
    if isinstance(failure, EIAClientError):
        assert error["retryable"] == failure.retryable


def test_actual_writer_contention_is_reported_as_exit_five(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("EIA_API_KEY", SECRET)
    monkeypatch.setattr(cli, "EIAClient", lambda key: FakeClient())
    with writer_lock(tmp_path):
        assert cli.main(["ingest", "--data-dir", str(tmp_path)]) == 5
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "already_running"


def test_json_log_formatter_redacts_messages_and_allowed_extra_fields():
    formatter = cli.JsonLogFormatter(SECRET)
    record = logging.LogRecord(
        "outage_explorer", logging.ERROR, __file__, 1, "Unsafe source message %s", (SECRET,), None
    )
    record.route = SECRET
    record.offset = 0
    result = formatter.format(record)
    assert SECRET not in result
    assert json.loads(result)["offset"] == 0
