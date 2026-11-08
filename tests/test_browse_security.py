"""Keep the structured native reader inside existing HTTP identity and admission policy."""

from __future__ import annotations

import json
from contextvars import Context
from types import SimpleNamespace

import pytest
from django.contrib.sessions.models import Session
from django.core import signing
from django.test import Client

from outage_explorer.analytics.model_contracts import DATASETS
from outage_explorer.query.browse_runtime import BrowseRuntimeError
from outage_explorer.service import admission, api_services


def send(client, payload=None, dataset="fleet_daily"):
    return client.post(
        f"/api/v1/data/{dataset}", json.dumps(payload or {}), content_type="application/json"
    )


def synthetic_page(*_args, **_kwargs):
    """Return a sentinel-bearing page without replacing HTTP, policy or cursor behavior."""
    return {
        "columns": [{"name": name, "type": kind} for name, kind in DATASETS["fleet_daily"].columns],
        "rows": [
            [
                period,
                "100.250000000",
                "0.000000000",
                "0.000000000",
                "0.000000000000",
                "0.000000000",
                "0.000000000",
                "[]",
            ]
            for period in ("2026-09-01", "2026-09-02")
        ],
    }


def forbid_io(*_args, **_kwargs):
    pytest.fail("Authorization, request validation or cursor context must reject before I/O")


@pytest.mark.django_db
def test_anonymous_json_browse_is_denied_before_model_or_native_access(api_context, monkeypatch):
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    client.defaults["HTTP_X_CSRFTOKEN"] = token
    monkeypatch.setattr(api_services, "_bundle", forbid_io)
    monkeypatch.setattr(api_services, "run_browse", forbid_io)
    response = send(client)
    assert response.status_code == 401 and response.json()["code"] == "authentication_required"


def test_viewer_detail_denial_precedes_even_structured_column_validation(
    authenticated_client, monkeypatch
):
    client, _ = authenticated_client("viewer")
    monkeypatch.setattr(api_services, "normalize_query", forbid_io)
    monkeypatch.setattr(api_services, "_bundle", forbid_io)
    response = send(
        client, {"filters": [{"column": "PRIVATE_CANARY", "op": "invalid"}]}, "generator_daily"
    )
    assert response.status_code == 403 and response.json()["code"] == "dataset_forbidden"
    assert "PRIVATE_CANARY" not in response.content.decode()


@pytest.mark.parametrize(
    "payload",
    [
        {"sort": "PRIVATE_CANARY"},
        {"filters": [{"column": "PRIVATE_CANARY", "op": "eq", "value": "x"}]},
        {"filters": [{"column": "period", "op": "contains", "value": "PRIVATE_CANARY"}]},
    ],
)
def test_invalid_columns_and_typed_operators_fail_before_model_io(
    authenticated_client, monkeypatch, payload
):
    client, _ = authenticated_client("viewer")
    monkeypatch.setattr(api_services, "_bundle", forbid_io)
    monkeypatch.setattr(api_services, "run_browse", forbid_io)
    response = send(client, payload)
    assert response.status_code == 400 and response.json()["code"] == "invalid_browse"
    assert "PRIVATE_CANARY" not in response.content.decode()


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/v1/data/fleet_daily", {}),
        ("/api/v1/query", {"dataset_id": "fleet_daily", "sql": "SELECT period FROM fleet_daily"}),
    ],
)
def test_browse_and_user_sql_share_the_same_account_admission_lock(
    authenticated_client, monkeypatch, path, payload
):
    client, user = authenticated_client("viewer")
    monkeypatch.setattr(api_services, "_bundle", forbid_io)
    with admission.query_slot(user.pk):
        response = Context().run(
            lambda: client.post(path, json.dumps(payload), content_type="application/json")
        )
    assert response.status_code == 429 and response.json()["code"] == "user_query_busy"


@pytest.mark.parametrize("elapsed", [3.25, 8.25])
def test_native_browse_receives_only_the_deadline_left_after_model_resolution(
    authenticated_client, monkeypatch, elapsed
):
    client, _ = authenticated_client("viewer")
    bundle = api_services._bundle(None)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(admission, "time", SimpleNamespace(monotonic=lambda: clock.now))

    def resolve(_model):
        clock.now += elapsed
        return bundle

    captured = []

    def native(*args, **kwargs):
        captured.append(kwargs["timeout_seconds"])
        return synthetic_page()

    monkeypatch.setattr(api_services, "_bundle", resolve)
    monkeypatch.setattr(api_services, "run_browse", native)
    response = send(client, {"limit": 1})
    if elapsed < 8:
        assert response.status_code == 200 and captured == [8 - elapsed]
    else:
        assert response.status_code == 504 and response.json()["code"] == "query_timeout"
        assert captured == []
    assert admission._ACTIVE.get() is None


def test_browse_cursor_cannot_cross_a_new_session_of_the_same_user_or_another_account(
    authenticated_client, monkeypatch
):
    first, _ = authenticated_client("viewer")
    monkeypatch.setattr(api_services, "run_browse", synthetic_page)
    initial = send(first, {"limit": 1})
    assert initial.status_code == 200
    token = initial.json()["next_cursor"]
    same_user, _ = authenticated_client("viewer")
    other_user, _ = authenticated_client("analyst")
    monkeypatch.setattr(api_services, "_bundle", forbid_io)
    for client in (same_user, other_user):
        response = send(client, {"limit": 1, "cursor": token})
        assert response.status_code == 400 and response.json()["code"] == "invalid_cursor"


@pytest.mark.parametrize("changed", [{"source": "f" * 32}, {"purpose": "query"}])
def test_browse_cursor_source_and_purpose_are_checked_before_native_execution(
    authenticated_client, monkeypatch, changed
):
    client, _ = authenticated_client("viewer")
    monkeypatch.setattr(api_services, "run_browse", synthetic_page)
    first = send(client, {"limit": 1})
    token = signing.loads(first.json()["next_cursor"], salt=api_services.CURSOR_SALT)
    forged = api_services._cursor({**token, **changed})
    monkeypatch.setattr(api_services, "run_browse", forbid_io)
    response = send(client, {"limit": 1, "cursor": forged})
    assert response.status_code == 400 and response.json()["code"] == "invalid_cursor"


@pytest.mark.parametrize(
    "code,status,public_code",
    [
        ("query_timeout", 504, "query_timeout"),
        ("result_too_large", 413, "result_too_large"),
        ("data_integrity", 503, "data_integrity"),
        ("runtime_unavailable", 503, "browse_unavailable"),
        ("resource_limit", 422, "query_failed"),
    ],
)
def test_native_failures_expose_only_static_problem_details(
    authenticated_client, monkeypatch, caplog, code, status, public_code
):
    client, _ = authenticated_client("viewer")

    def fail(*_args, **_kwargs):
        error = BrowseRuntimeError(code)
        error.args = ("PRIVATE_PATH_SQL_KEY_CANARY",)
        raise error

    monkeypatch.setattr(api_services, "run_browse", fail)
    response = send(client)
    assert response.status_code == status and response.json()["code"] == public_code
    assert response["Content-Type"].startswith("application/problem+json")
    assert "PRIVATE_PATH_SQL_KEY_CANARY" not in response.content.decode() + caplog.text
    assert not {"rows", "columns", "model_id", "source_snapshot_id", "query"} & set(response.json())


def test_session_revocation_after_native_read_prevents_delivery(authenticated_client, monkeypatch):
    client, _ = authenticated_client("viewer")
    key = client.session.session_key

    def revoke(*_args, **_kwargs):
        Session.objects.filter(session_key=key).delete()
        return synthetic_page()

    monkeypatch.setattr(api_services, "run_browse", revoke)
    response = send(client)
    assert response.status_code == 401 and response.json()["code"] == "session_revoked"
    assert "rows" not in response.json()


def test_compressible_oversized_sort_key_cannot_emit_an_unusable_signed_cursor(
    authenticated_client, monkeypatch
):
    client, _ = authenticated_client("viewer")

    def native(*_args, **_kwargs):
        page = synthetic_page()
        page["rows"][0][-1] = "A" * 16385
        page["rows"][1][-1] = "B" * 16385
        return page

    monkeypatch.setattr(api_services, "run_browse", native)
    response = send(client, {"sort": "quality_flags", "limit": 1})
    assert response.status_code == 413 and response.json()["code"] == "browse_cursor_too_large"
    assert "next_cursor" not in response.json()
