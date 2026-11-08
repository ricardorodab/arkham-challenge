"""Real Django/WASM behavior, snapshot consistency and bounded result delivery."""

from __future__ import annotations

import json
import shutil
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from outage_explorer.data.pipeline import IngestConfig, ingest
from outage_explorer.query.query_sandbox import SandboxError
from outage_explorer.service import api_services
from outage_explorer.service.models import Persona, QueryCacheEntry, RefreshJob

from .helpers import FakeClient


def post(client, url, payload):
    return client.post(url, json.dumps(payload), content_type="application/json")


def test_viewer_catalog_hides_detail_metadata_and_paths(authenticated_client):
    client, _ = authenticated_client("viewer")
    result = client.get("/api/v1/catalog")
    assert result.status_code == 200, result.content
    payload = result.json()
    assert {x["id"] for x in payload["datasets"]} == {"national_daily", "fleet_daily"}
    rendered = result.content.decode()
    for forbidden in ("facility_daily", "generator_daily", "dim_facility", ".parquet", "D:\\"):
        assert forbidden not in rendered
    assert payload["metrics"]["fleet_offline_share"]["version"] == 1
    assert result["Cache-Control"] == "no-store"
    assert len(result["X-Request-ID"]) == 32


def test_analyst_catalog_exposes_typed_ten_dataset_contract(authenticated_client):
    client, _ = authenticated_client("analyst")
    payload = client.get("/api/v1/catalog").json()
    assert len(payload["datasets"]) == 10
    generator = next(x for x in payload["datasets"] if x["id"] == "generator_daily")
    assert generator["primary_key"] == ["period", "facility_id", "generator_id"]
    assert ["capacity_mw", "DECIMAL(24,9)"] in generator["schema"]


def test_preview_filters_real_synthetic_parquet_and_preserves_decimal_strings(authenticated_client):
    client, _ = authenticated_client("analyst")
    result = client.get(
        "/api/v1/data/generator_daily",
        {"start": "2026-09-02", "end": "2026-09-02", "facility_id": "001"},
    )
    assert result.status_code == 200, result.content
    payload = result.json()
    assert payload["returned_rows"] == 2
    names = [x["name"] for x in payload["columns"]]
    for row in payload["rows"]:
        assert row[names.index("period")] == "2026-09-02"
        assert row[names.index("facility_id")] == "001"
        assert row[names.index("capacity_mw")] == "100.250000000"
    assert payload["next_cursor"] is None


def test_preview_keyset_cursor_reads_each_observation_once(authenticated_client):
    client, _ = authenticated_client("viewer")
    args = {"limit": 1}
    periods = []
    versions = []
    while True:
        result = client.get("/api/v1/data/fleet_daily", args)
        assert result.status_code == 200, result.content
        payload = result.json()
        periods.append(payload["rows"][0][0])
        versions.append(payload["model_id"])
        if payload["next_cursor"] is None:
            break
        args["cursor"] = payload["next_cursor"]
    assert periods == ["2026-09-01", "2026-09-02", "2026-09-03"]
    assert len(set(versions)) == 1


def test_cursor_pins_prior_model_after_refresh(
    authenticated_client, api_context, api_data_dir, tmp_path
):
    target = tmp_path / "refreshing-data"
    shutil.copytree(api_data_dir, target)
    api_context.OUTAGE_DATA_DIR = target
    client, _ = authenticated_client("viewer")
    first = client.get("/api/v1/data/fleet_daily", {"limit": 1}).json()
    ingest(IngestConfig(target, "recent"), FakeClient())
    current = client.get("/api/v1/catalog").json()
    assert current["model_id"] != first["model_id"]
    second = client.get("/api/v1/data/fleet_daily", {"limit": 1, "cursor": first["next_cursor"]})
    assert second.status_code == 200, second.content
    assert second.json()["model_id"] == first["model_id"]
    assert second.json()["rows"][0][0] == "2026-09-02"


def test_supported_aggregate_runs_in_wasm_with_exact_decimal_output(authenticated_client):
    client, _ = authenticated_client("analyst")
    result = post(
        client,
        "/api/v1/query",
        {
            "dataset_id": "generator_daily",
            "sql": "SELECT facility_id, SUM(capacity_mw) AS capacity FROM generator_daily GROUP BY facility_id ORDER BY facility_id",
        },
    )
    assert result.status_code == 200, result.content
    payload = result.json()
    assert payload["returned_rows"] == 2
    assert payload["rows"][0][0] == "001"
    assert Decimal(payload["rows"][0][1]) == Decimal("601.5")
    assert not payload["truncated"]


def test_query_cap_pagination_cache_and_expiration(authenticated_client, monkeypatch):
    client, _ = authenticated_client("viewer")
    body = {
        "dataset_id": "fleet_daily",
        "sql": "SELECT period,offline_pct FROM fleet_daily",
        "max_rows": 2,
        "page_size": 1,
    }
    first = post(client, "/api/v1/query", body)
    assert first.status_code == 200, first.content
    payload = first.json()
    assert payload["result_rows"] == 2 and payload["truncated"]
    assert not payload["cache_hit"] and payload["next_cursor"]
    second = post(client, "/api/v1/query", {**body, "cursor": payload["next_cursor"]}).json()
    assert second["cache_hit"] and second["next_cursor"] is None
    assert second["rows"][0][0] == "2026-09-02"
    original = api_services._run
    calls = []

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(api_services, "_run", counted)
    QueryCacheEntry.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    again = post(client, "/api/v1/query", body)
    assert again.status_code == 200, again.content
    assert not again.json()["cache_hit"] and len(calls) == 1


def test_refresh_generation_invalidates_result_cache(
    authenticated_client, api_context, api_data_dir, tmp_path
):
    target = tmp_path / "cache-refresh-data"
    shutil.copytree(api_data_dir, target)
    api_context.OUTAGE_DATA_DIR = target
    client, _ = authenticated_client("viewer")
    body = {"dataset_id": "fleet_daily", "sql": "SELECT period FROM fleet_daily"}
    first = post(client, "/api/v1/query", body).json()
    assert post(client, "/api/v1/query", body).json()["cache_hit"]
    ingest(IngestConfig(target, "recent"), FakeClient())
    new = post(client, "/api/v1/query", body)
    assert new.status_code == 200, new.content
    assert not new.json()["cache_hit"]
    assert new.json()["model_id"] != first["model_id"]


@pytest.mark.parametrize(
    "code,status,public_code",
    [
        ("query_timeout", 504, "query_timeout"),
        ("result_too_large", 413, "result_too_large"),
        ("sandbox_unavailable", 503, "sandbox_unavailable"),
        ("query_failed", 422, "query_failed"),
    ],
)
def test_worker_failures_have_consistent_safe_http_errors(
    authenticated_client, monkeypatch, code, status, public_code
):
    client, _ = authenticated_client("viewer")

    def fail(*args, **kwargs):
        raise SandboxError(code)

    monkeypatch.setattr(api_services, "run_query", fail)
    result = post(
        client, "/api/v1/query", {"dataset_id": "fleet_daily", "sql": "SELECT * FROM fleet_daily"}
    )
    assert result.status_code == status
    assert result.json()["code"] == public_code
    assert result["Content-Type"].startswith("application/problem+json")
    assert result.json()["request_id"] == result["X-Request-ID"]


def test_admin_refresh_is_durable_idempotent_and_polled(authenticated_client):
    client, _ = authenticated_client("admin")
    result = client.post(
        "/api/v1/refresh",
        '{"mode":"recent"}',
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="synthetic-acceptance-1",
    )
    assert result.status_code == 202, result.content
    job = result.json()
    assert job["status"] == "queued" and job["progress"] == {}
    replay = client.post(
        "/api/v1/refresh",
        '{"mode":"recent"}',
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="synthetic-acceptance-1",
    )
    assert replay.status_code == 200 and replay.json()["id"] == job["id"]
    assert client.get(job["status_url"]).json()["status"] == "queued"
    assert len(client.get("/api/v1/refresh").json()["jobs"]) == 1
    assert post(client, "/api/v1/refresh", {"mode": "full"}).status_code == 409


def test_refresh_progress_is_persistent_and_filtered_on_status_history_and_replay(
    authenticated_client,
):
    """Expose the same safe snapshot without replacing failed/interrupted work by estimates."""
    client, _ = authenticated_client("admin")
    response = client.post(
        "/api/v1/refresh",
        '{"mode":"recent"}',
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY="synthetic-progress-replay",
    )
    assert response.status_code == 202
    job = RefreshJob.objects.get(pk=response.json()["id"])
    status_url = response.json()["status_url"]
    job.status = "succeeded"
    job.save(update_fields=["status"])
    assert client.get(status_url).json()["progress"] == {}
    for status, stage in (
        ("running", "extracting"),
        ("failed", "extracting"),
        ("interrupted", "validating_model"),
        ("running", "exporting_model"),
        ("succeeded", "completed"),
    ):
        job.status = status
        job.progress = {
            "stage": stage,
            "updated_at": "2026-11-08T09:45:00+05:30",
            "rows_received": 42,
            "pages_fetched": 2,
            "route": "generator",
            "routes": {
                "national": {"rows_received": 2, "pages_fetched": 1, "secret": "PRIVATE_CANARY"},
                "generator": {"rows_received": 40, "pages_fetched": 1},
                "PRIVATE_CANARY": {"rows_received": 7, "pages_fetched": 1},
            },
            "completed": 3,
            "total": 10,
            "api_key": "PRIVATE_CANARY",
            "path": "PRIVATE_CANARY",
            "sql": "PRIVATE_CANARY",
        }
        job.save(update_fields=["status", "progress"])
        expected = {
            "stage": stage,
            "updated_at": "2026-11-08T04:15:00+00:00",
            "rows_received": 42,
            "pages_fetched": 2,
            "route": "generator",
            "routes": {
                "national": {"rows_received": 2, "pages_fetched": 1},
                "generator": {"rows_received": 40, "pages_fetched": 1},
            },
        }
        if stage == "exporting_model":
            expected.update(completed=3, total=10)
        polled = client.get(status_url)
        history = client.get("/api/v1/refresh")
        replay = client.post(
            "/api/v1/refresh",
            '{"mode":"recent"}',
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="synthetic-progress-replay",
        )
        assert polled.status_code == history.status_code == replay.status_code == 200
        for payload in (polled.json(), history.json()["jobs"][0], replay.json()):
            assert payload["id"] == str(job.id) and payload["status"] == status
            assert payload["progress"] == expected
            assert "PRIVATE_CANARY" not in json.dumps(payload)
        job.refresh_from_db()
        assert job.progress["api_key"] == "PRIVATE_CANARY"


def test_refresh_progress_malformed_snapshots_and_counters_are_safe_at_the_http_boundary(
    authenticated_client,
):
    client, owner = authenticated_client("admin")
    job = RefreshJob.objects.create(owner=owner, mode="full", status="running")
    stamp = "2026-11-08T04:15:00+00:00"
    for malformed in (
        [],
        {"stage": ["extracting"], "updated_at": stamp},
        {"stage": "PRIVATE_CANARY", "updated_at": stamp},
        {"stage": "extracting"},
        {"stage": "extracting", "updated_at": "2026-11-08T04:15:00"},
        {"stage": "extracting", "updated_at": "PRIVATE_CANARY"},
    ):
        job.progress = malformed
        job.save(update_fields=["progress"])
        response = client.get(f"/api/v1/refresh/{job.id}")
        assert response.status_code == 200
        assert response.json()["progress"] == {}
    job.progress = {
        "stage": "extracting",
        "updated_at": stamp,
        "rows_received": True,
        "pages_fetched": -1,
        "route": ["generator"],
        "routes": {
            "national": {"rows_received": 1.5, "pages_fetched": 1 << 53},
            "facility": [],
        },
        "completed": 2,
        "total": 10,
    }
    job.save(update_fields=["progress"])
    assert client.get(f"/api/v1/refresh/{job.id}").json()["progress"] == {
        "stage": "extracting",
        "updated_at": stamp,
        "rows_received": 0,
        "pages_fetched": 0,
        "routes": {"national": {"rows_received": 0, "pages_fetched": 0}},
    }
    for completed, total, valid in (
        (0, 10, True),
        (10, 10, True),
        (11, 10, False),
        (1, 9, False),
        (True, 10, False),
    ):
        job.progress = {
            "stage": "exporting_model",
            "updated_at": stamp,
            "completed": completed,
            "total": total,
            "rows_received": (1 << 53) - 1,
        }
        job.save(update_fields=["progress"])
        payload = client.get(f"/api/v1/refresh/{job.id}").json()["progress"]
        assert payload["rows_received"] == (1 << 53) - 1
        assert ("completed" in payload) is valid and ("total" in payload) is valid
        if valid:
            assert payload["completed"] == completed and payload["total"] == total


def test_refresh_progress_remains_private_to_its_current_admin_owner(
    authenticated_client, api_users
):
    owner_client, owner = authenticated_client("admin")
    Persona.objects.filter(user=api_users["analyst2"]).update(role="admin")
    other_client, _ = authenticated_client("analyst2")
    job = RefreshJob.objects.create(
        owner=owner,
        mode="full",
        status="running",
        progress={
            "stage": "extracting",
            "updated_at": "2026-11-08T04:15:00+00:00",
            "rows_received": 42,
        },
    )
    path = f"/api/v1/refresh/{job.id}"
    assert owner_client.get(path).json()["progress"]["rows_received"] == 42
    hidden = other_client.get(path)
    assert hidden.status_code == 404 and hidden.json()["code"] == "job_not_found"
    assert other_client.get("/api/v1/refresh").json() == {"jobs": []}
    for role in ("viewer", "analyst"):
        client, _ = authenticated_client(role)
        denied = client.get(path)
        assert denied.status_code == 403 and denied.json()["code"] == "admin_required"
        assert "progress" not in denied.json() and "rows_received" not in denied.content.decode()


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2026-99-01"},
        {"start": "2026-10-01", "end": "2026-09-01"},
        {"facility_id": "001"},
        {"limit": 1001},
        {"limit": 0},
        {"cursor": "invalid"},
    ],
)
def test_preview_validation_fails_before_execution(authenticated_client, monkeypatch, params):
    client, _ = authenticated_client("viewer")

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid preview must not execute")

    monkeypatch.setattr(api_services, "_run_browse", forbidden)
    result = client.get("/api/v1/data/fleet_daily", params)
    assert result.status_code == 400


def test_no_observations_is_an_empty_typed_result(authenticated_client):
    client, _ = authenticated_client("viewer")
    result = client.get("/api/v1/data/fleet_daily", {"start": "2020-01-01", "end": "2020-01-02"})
    assert result.status_code == 200, result.content
    assert result.json()["rows"] == []
    assert result.json()["columns"] and result.json()["next_cursor"] is None
