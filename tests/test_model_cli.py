"""Model administration operates offline and emits version/source-bound JSON."""

from __future__ import annotations

import json
import logging

import pytest

from outage_explorer import cli
from outage_explorer.model_contracts import MODEL_VERSION
from outage_explorer.storage import Catalog
from tests.test_cli import forbid_client
from tests.test_model_store import seed


@pytest.fixture(autouse=True)
def offline_cli(monkeypatch):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.setattr(cli, "EIAClient", forbid_client)
    logger = logging.getLogger("outage_explorer")
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    yield
    for handler in logger.handlers:
        if handler not in handlers:
            handler.close()
    logger.handlers, logger.level, logger.propagate = handlers, level, propagate


def invoke(capsys, root, action, *arguments):
    code = cli.main(["model", action, "--data-dir", str(root), *arguments])
    captured = capsys.readouterr()
    result = json.loads(captured.out) if captured.out else None
    return code, result, captured.err


def test_model_build_status_history_and_verification_persist_source_binding(tmp_path, capsys):
    before = seed(tmp_path)
    capsys.readouterr()
    code, built, _ = invoke(capsys, tmp_path, "build", "--log-level", "ERROR")
    assert code == 0 and built["model_id"] != before.id
    assert built["model_version"] == MODEL_VERSION
    assert built["source_snapshot_id"] == before.manifest["source_snapshot_id"]
    assert len(built["datasets"]) == 10
    with Catalog(tmp_path) as catalog:
        assert catalog.current_model().id == built["model_id"]
    code, status, error = invoke(capsys, tmp_path, "status")
    assert code == 0 and error == ""
    assert status["current_model"] == built["model_id"]
    assert status["manifest"]["source_snapshot_id"] == built["source_snapshot_id"]
    code, history, _ = invoke(capsys, tmp_path, "history", "--limit", "2")
    assert code == 0 and len(history["builds"]) == 2
    assert history["builds"][0]["model_id"] == built["model_id"]
    assert all(row["status"] == "succeeded" for row in history["builds"])
    code, verified, _ = invoke(capsys, tmp_path, "verify", "--model-id", before.id)
    assert code == 0 and verified["verified"] is True
    assert verified["model_id"] == before.id


def test_model_catalog_viewer_has_only_national_data_and_correct_percentage_units(tmp_path, capsys):
    bundle = seed(tmp_path)
    capsys.readouterr()
    code, result, error = invoke(capsys, tmp_path, "catalog", "--persona", "viewer")
    assert code == 0 and error == ""
    assert result["model_id"] == bundle.id
    assert {entry["id"] for entry in result["datasets"]} == {"national_daily", "fleet_daily"}
    fleet = next(entry for entry in result["datasets"] if entry["id"] == "fleet_daily")
    assert fleet["units"]["offline_pct"] == "percent"
    assert fleet["units"]["reported_vs_calculated_pct_gap"] == "percentage points"
    assert all("path" not in entry and "file" not in entry for entry in result["datasets"])


@pytest.mark.parametrize("action", ["status", "catalog", "verify", "report"])
def test_model_commands_reject_unknown_historical_identifier_without_api_key(
    tmp_path, capsys, action
):
    seed(tmp_path)
    capsys.readouterr()
    code, result, error = invoke(capsys, tmp_path, action, "--model-id", "f" * 32)
    assert code == 4 and result is None
    assert json.loads(error)["error"]["code"] == "no_model"


def test_build_without_raw_snapshot_is_operational_error_and_status_is_valid(tmp_path, capsys):
    code, result, error = invoke(capsys, tmp_path, "build", "--log-level", "ERROR")
    assert code == 4 and result is None
    assert json.loads(error)["error"]["code"] == "no_snapshot"
    code, result, error = invoke(capsys, tmp_path, "status")
    assert code == 0 and result == {"current_model": None, "manifest": None} and error == ""


def test_report_defaults_to_thirty_days_and_writes_json_outside_data_directory(tmp_path, capsys):
    root = tmp_path / "data"
    bundle = seed(root)
    capsys.readouterr()
    output = tmp_path / "results" / "report.json"
    code, summary, error = invoke(capsys, root, "report", "--output", str(output))
    assert code == 0 and error == ""
    assert summary["output"] == str(output.resolve())
    assert summary["model_id"] == bundle.id
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["reconciliation_start"] == "2026-09-01"
    assert saved["reconciliation_end"] == "2026-09-30"
    assert saved["requested_calendar_days"] == 30
    assert len(saved["queries"]) == 18
    assert summary["queries"]["daily_national_reconciliation"] == 30
    first = saved["queries"]["daily_national_reconciliation"]["rows"][0]
    assert first[0] == "2026-09-01" and "100.250000000" in first


def test_report_stdout_is_serializable_without_an_output_file(tmp_path, capsys):
    bundle = seed(tmp_path)
    capsys.readouterr()
    code, result, error = invoke(capsys, tmp_path, "report")
    assert code == 0 and error == ""
    assert result["model_id"] == bundle.id
    assert result["requested_calendar_days"] == 30
    assert result["queries"]["daily_national_reconciliation"]["row_count"] == 30


def test_report_cannot_write_inside_immutable_data_directory(tmp_path, capsys):
    bundle = seed(tmp_path)
    capsys.readouterr()
    manifest = bundle.path / "manifest.json"
    original = manifest.read_bytes()
    code, result, error = invoke(capsys, tmp_path, "report", "--output", str(manifest))
    assert code == 2 and result is None
    assert json.loads(error)["error"]["code"] == "configuration"
    assert manifest.read_bytes() == original


def test_short_report_range_returns_configuration_error_before_data_access(tmp_path, capsys):
    code, result, error = invoke(
        capsys, tmp_path, "report", "--start", "2026-09-01", "--end", "2026-09-29"
    )
    assert code == 2 and result is None
    assert "30 calendar days" in json.loads(error)["error"]["message"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("option", ["--sql-file", "--query"])
def test_cli_does_not_accept_user_supplied_report_sql(tmp_path, capsys, option):
    with pytest.raises(SystemExit) as caught:
        cli.main(["model", "report", "--data-dir", str(tmp_path), option, "untrusted.sql"])
    assert caught.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
