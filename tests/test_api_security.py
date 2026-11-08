"""Exercise authorization and browser/session boundaries before analytical I/O."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from outage_explorer.analytics.model_contracts import DATASETS
from outage_explorer.query.query_sandbox import SandboxError
from outage_explorer.service import api_services
from outage_explorer.service.models import Persona, QueryCacheEntry, RefreshJob
from tests.conftest import API_PASSWORD

pytestmark = pytest.mark.django_db
QUERY = "/api/v1/query"
BODY = {"dataset_id": "fleet_daily", "sql": "SELECT period FROM fleet_daily", "page_size": 1}
RESULT = {
    "columns": [{"name": "period", "type": "DATE"}],
    "rows": [["2026-09-01"], ["2026-09-02"], ["2026-09-03"]],
}


@pytest.fixture(autouse=True)
def isolated_api(api_context):
    pass


@pytest.fixture
def no_analytical_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Authorization/validation must finish before analytical model or query I/O.")

    monkeypatch.setattr(api_services, "_bundle", forbidden)
    monkeypatch.setattr(api_services, "_run", forbidden)
    monkeypatch.setattr(api_services, "_run_browse", forbidden)


def post(client, payload, path=QUERY, **headers):
    return client.post(path, json.dumps(payload), content_type="application/json", **headers)


def denied(response, status, code=None):
    assert response.status_code == status, response.content
    assert response["Content-Type"].startswith("application/problem+json")
    payload = response.json()
    if code:
        assert payload["code"] == code
    assert not ({"rows", "columns", "model_id", "source_snapshot_id", "path"} & set(payload))
    return payload


def anonymous_client():
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    client.defaults["HTTP_X_CSRFTOKEN"] = token
    return client


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/catalog",
        "/api/v1/data/fleet_daily",
        "/api/v1/data/generator_daily",
        "/api/v1/refresh",
        "/api/v1/health/ready",
        "/api/v1/openapi.json",
    ],
)
def test_anonymous_gets_are_denied_before_analytical_access(no_analytical_io, path):
    denied(anonymous_client().get(path), 401, "authentication_required")


def test_anonymous_query_cannot_read_even_national_data(no_analytical_io):
    denied(post(anonymous_client(), BODY), 401, "authentication_required")


@pytest.mark.parametrize(
    "dataset", [name for name, spec in DATASETS.items() if spec.classification != "national"]
)
def test_viewer_cannot_preview_any_detail_dataset_before_model_access(
    authenticated_client, no_analytical_io, dataset
):
    client, _ = authenticated_client("viewer")
    response = client.get(f"/api/v1/data/{dataset}")
    denied(response, 403, "dataset_forbidden")
    assert dataset not in response.content.decode()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM generator_daily",
        "SELECT * FROM fleet_daily WHERE EXISTS(SELECT 1 FROM generator_daily)",
        "SELECT (SELECT max(capacity_mw) FROM generator_daily) FROM fleet_daily",
        "WITH private AS(SELECT * FROM generator_daily) SELECT * FROM fleet_daily",
        "SELECT f.* FROM fleet_daily f JOIN generator_daily g ON f.period=g.period",
        "SELECT * FROM fleet_daily UNION ALL SELECT * FROM generator_daily",
        'SELECT * FROM main."GENERATOR_DAILY"',
    ],
)
def test_every_nested_or_joined_reference_is_authorized_before_model_access(
    authenticated_client, no_analytical_io, sql
):
    client, _ = authenticated_client("viewer")
    denied(post(client, {**BODY, "sql": sql}), 403, "dataset_forbidden")


def test_selected_detail_dataset_is_denied_before_parsing_attacker_sql(
    authenticated_client, no_analytical_io, monkeypatch
):
    client, _ = authenticated_client("viewer")
    monkeypatch.setattr(
        api_services,
        "compile_sql",
        lambda *_: pytest.fail("Do not parse unauthorized dataset SQL."),
    )
    denied(
        post(client, {"dataset_id": "generator_daily", "sql": "not valid SQL"}),
        403,
        "dataset_forbidden",
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"HTTP_X_ROLE": "admin"},
        {"HTTP_X_PERSONA": "analyst"},
        {"HTTP_AUTHORIZATION": "Bearer admin"},
        {"HTTP_X_USER": "admin"},
    ],
)
def test_role_headers_do_not_override_the_server_persona(
    authenticated_client, no_analytical_io, headers
):
    client, _ = authenticated_client("viewer")
    denied(client.get("/api/v1/data/generator_daily", **headers), 403, "dataset_forbidden")


@pytest.mark.parametrize(
    "spoof", [{"role": "admin"}, {"persona": "admin"}, {"user_id": 1}, {"username": "admin"}]
)
def test_role_or_user_fields_are_rejected_in_query_bodies(
    authenticated_client, no_analytical_io, spoof
):
    client, _ = authenticated_client("viewer")
    denied(post(client, {**BODY, **spoof}), 400, "invalid_json")


def test_django_staff_flags_do_not_override_the_product_persona(
    authenticated_client, no_analytical_io
):
    client, user = authenticated_client("viewer")
    get_user_model().objects.filter(pk=user.pk).update(is_staff=True, is_superuser=True)
    denied(client.get("/api/v1/data/generator_daily"), 403, "dataset_forbidden")
    denied(post(client, {"mode": "recent"}, "/api/v1/refresh"), 403, "admin_required")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM fleet_daily",
        "COPY fleet_daily TO 'secret.csv'",
        "INSTALL httpfs",
        "SELECT * FROM read_parquet('https://example.com/private.parquet')",
        "SELECT * FROM read_csv_auto('D:/private/key.txt')",
        "SELECT * FROM query_table('fleet_daily')",
        "SELECT current_setting('external_access') FROM fleet_daily",
        "SELECT * FROM fleet_daily; SELECT * FROM fleet_daily",
        "SELECT * FROM (SELECT * FROM fleet_daily) nested",
        "SELECT row_number() OVER() FROM fleet_daily",
    ],
)
def test_writes_external_functions_and_unsupported_query_shapes_never_open_a_model(
    authenticated_client, no_analytical_io, sql
):
    client, _ = authenticated_client("analyst")
    denied(post(client, {**BODY, "sql": sql}), 422)


@pytest.mark.parametrize("change", ["inactive", "password", "missing_persona"])
def test_existing_session_is_revoked_or_denied_after_account_change(
    authenticated_client, no_analytical_io, change
):
    client, user = authenticated_client("analyst")
    if change == "inactive":
        get_user_model().objects.filter(pk=user.pk).update(is_active=False)
    elif change == "password":
        user.set_password("A-Different-Synthetic-Password-987!")
        user.save(update_fields=["password"])
    else:
        Persona.objects.filter(user=user).delete()
    denied(post(client, BODY), 403 if change == "missing_persona" else 401)


@pytest.mark.parametrize("change", ["inactive", "missing_persona"])
def test_login_fails_closed_for_ineligible_accounts(api_users, no_analytical_io, change):
    user = api_users["analyst"]
    if change == "inactive":
        get_user_model().objects.filter(pk=user.pk).update(is_active=False)
    else:
        Persona.objects.filter(user=user).delete()
    denied(
        post(
            anonymous_client(),
            {"username": "analyst", "password": API_PASSWORD},
            "/api/v1/auth/login",
        ),
        401,
        "invalid_credentials",
    )


def test_unknown_persona_from_authentication_backend_is_denied(
    api_users, no_analytical_io, monkeypatch
):
    from outage_explorer.service import views

    user = api_users["analyst"]
    user.persona.role = "operator"  # A misconfigured backend must fail closed too.
    monkeypatch.setattr(views, "authenticate", lambda *args, **kwargs: user)
    denied(
        post(
            anonymous_client(),
            {"username": "analyst", "password": API_PASSWORD},
            "/api/v1/auth/login",
        ),
        401,
        "invalid_credentials",
    )


@pytest.mark.parametrize(
    "path", ["/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/query", "/api/v1/refresh"]
)
def test_mutations_require_csrf_even_with_a_valid_session(
    authenticated_client, no_analytical_io, path
):
    client, _ = authenticated_client("admin")
    client.defaults.pop("HTTP_X_CSRFTOKEN")
    payload = {"username": "admin", "password": API_PASSWORD} if path.endswith("login") else {}
    denied(post(client, payload, path), 403, "csrf_failed")
    assert client.get("/api/v1/auth/session").status_code == 200


def test_login_requires_csrf_before_authentication_or_credential_processing(
    api_users, no_analytical_io
):
    client = Client(enforce_csrf_checks=True)
    denied(
        post(client, {"username": "admin", "password": API_PASSWORD}, "/api/v1/auth/login"),
        403,
        "csrf_failed",
    )
    assert "sessionid" not in client.cookies


def test_cross_origin_mutation_is_denied_even_with_valid_csrf_token(
    authenticated_client, no_analytical_io
):
    client, _ = authenticated_client("admin")
    denied(
        post(client, {"mode": "recent"}, "/api/v1/refresh", HTTP_ORIGIN="https://evil.invalid"),
        403,
        "csrf_failed",
    )
    assert RefreshJob.objects.count() == 0


def test_untrusted_host_is_denied_before_any_analytical_io(authenticated_client, no_analytical_io):
    client, _ = authenticated_client("analyst")
    response = client.get("/api/v1/catalog", HTTP_HOST="evil.invalid")
    assert response.status_code == 400


def test_query_cache_is_never_shared_between_users(authenticated_client, monkeypatch):
    first, first_user = authenticated_client("analyst")
    second, second_user = authenticated_client("analyst2")
    run = Mock(return_value=RESULT)
    monkeypatch.setattr(api_services, "_run", run)
    assert post(first, BODY).json()["cache_hit"] is False
    assert post(first, BODY).json()["cache_hit"] is True
    assert post(second, BODY).json()["cache_hit"] is False
    assert run.call_count == 2
    assert set(QueryCacheEntry.objects.values_list("owner_id", flat=True)) == {
        first_user.pk,
        second_user.pk,
    }


def test_query_cursor_cannot_be_replayed_by_another_user(authenticated_client, monkeypatch):
    first, _ = authenticated_client("analyst")
    second, _ = authenticated_client("analyst2")
    monkeypatch.setattr(api_services, "_run", Mock(return_value=RESULT))
    cursor = post(first, BODY).json()["next_cursor"]
    monkeypatch.setattr(
        api_services,
        "_bundle",
        lambda *_: pytest.fail("Reject actor-mismatched cursor before model resolution."),
    )
    denied(post(second, {**BODY, "cursor": cursor}), 400, "invalid_cursor")


def test_downgrade_blocks_detail_cursors_and_invalidates_old_national_cache(
    authenticated_client, monkeypatch
):
    client, user = authenticated_client("analyst")
    run = Mock(return_value=RESULT)
    monkeypatch.setattr(api_services, "_run", run)
    cursor = post(client, BODY).json()["next_cursor"]
    detail_body = {
        **BODY,
        "dataset_id": "generator_daily",
        "sql": "SELECT period FROM generator_daily",
    }
    detail_cursor = post(client, detail_body).json()["next_cursor"]
    Persona.objects.filter(user=user).update(role="viewer")
    original_bundle = api_services._bundle
    monkeypatch.setattr(
        api_services,
        "_bundle",
        lambda *_: pytest.fail("Reject revoked cursors before model resolution."),
    )
    denied(post(client, {**detail_body, "cursor": detail_cursor}), 403, "dataset_forbidden")
    denied(post(client, {**BODY, "cursor": cursor}), 400, "invalid_cursor")
    monkeypatch.setattr(api_services, "_bundle", original_bundle)
    fresh = post(client, BODY)
    assert fresh.status_code == 200 and fresh.json()["cache_hit"] is False
    assert run.call_count == 3
    assert set(QueryCacheEntry.objects.filter(owner=user).values_list("role", flat=True)) == {
        "analyst",
        "viewer",
    }


def test_permissions_are_rechecked_before_result_or_cache_publication(
    authenticated_client, monkeypatch
):
    client, user = authenticated_client("analyst")

    def revoke_during_execution(*args):
        Persona.objects.filter(user=user).update(role="viewer")
        return RESULT

    monkeypatch.setattr(api_services, "_run", revoke_during_execution)
    denied(post(client, BODY), 403, "permissions_changed")
    assert not QueryCacheEntry.objects.exists()


def test_preview_cursor_is_bound_to_its_owner_and_filters(authenticated_client, monkeypatch):
    first, _ = authenticated_client("analyst")
    second, _ = authenticated_client("analyst2")
    monkeypatch.setattr(api_services, "_run_browse", Mock(return_value=RESULT))
    response = first.get("/api/v1/data/fleet_daily?limit=1")
    assert response.status_code == 200, response.content
    cursor = response.json()["next_cursor"]
    monkeypatch.setattr(
        api_services,
        "_bundle",
        lambda *_: pytest.fail("Reject mismatched cursor before model resolution."),
    )
    denied(
        second.get("/api/v1/data/fleet_daily", {"limit": 1, "cursor": cursor}),
        400,
        "invalid_cursor",
    )
    denied(
        first.get("/api/v1/data/fleet_daily", {"limit": 2, "cursor": cursor}), 400, "invalid_cursor"
    )


def test_refresh_job_is_visible_only_to_its_admin_owner(authenticated_client, api_users):
    first, owner = authenticated_client("admin")
    job = RefreshJob.objects.create(owner=owner, mode="recent", status="succeeded")
    other = get_user_model().objects.create_user(username="admin2", password=API_PASSWORD)
    Persona.objects.create(user=other, role="admin")
    second = anonymous_client()
    login = post(second, {"username": "admin2", "password": API_PASSWORD}, "/api/v1/auth/login")
    assert login.status_code == 200
    second.defaults["HTTP_X_CSRFTOKEN"] = login.json()["csrf_token"]
    assert first.get(f"/api/v1/refresh/{job.id}").status_code == 200
    denied(second.get(f"/api/v1/refresh/{job.id}"), 404, "job_not_found")
    assert second.get("/api/v1/refresh").json()["jobs"] == []
    for role in ("viewer", "analyst"):
        client, _ = authenticated_client(role)
        denied(client.get(f"/api/v1/refresh/{job.id}"), 403, "admin_required")


def test_public_health_is_metadata_free_and_readiness_is_admin_only(
    authenticated_client, no_analytical_io
):
    response = Client().get("/api/v1/health/live")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    for role in ("viewer", "analyst"):
        client, _ = authenticated_client(role)
        denied(client.get("/api/v1/health/ready"), 403, "admin_required")


def test_public_health_revision_comes_from_deployment_configuration(settings, no_analytical_io):
    settings.OUTAGE_DEPLOY_REVISION = "a1" * 20
    result = Client().get("/api/v1/health/live", HTTP_X_OUTAGE_REVISION="spoofed")
    assert result.status_code == 200
    assert result.json() == {"status": "ok"}
    assert result["X-Outage-Revision"] == settings.OUTAGE_DEPLOY_REVISION
    assert result["Cache-Control"] == "no-store"
    settings.OUTAGE_DEPLOY_REVISION = ""
    assert "X-Outage-Revision" not in Client().get("/api/v1/health/live")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"dataset_id":"fleet_daily","dataset_id":"generator_daily","sql":"SELECT * FROM fleet_daily"}',
        b'{"dataset_id":"fleet_daily","sql":"SELECT * FROM fleet_daily","max_rows":NaN}',
        b"[]",
        b'{"dataset_id":"fleet_daily","sql":"SELECT * FROM fleet_daily","unexpected":true}',
        b"\xff",
    ],
)
def test_strict_json_parser_rejects_duplicates_unknown_fields_constants_and_invalid_text(
    authenticated_client, no_analytical_io, payload
):
    client, _ = authenticated_client("analyst")
    denied(client.post(QUERY, payload, content_type="application/json"), 400, "invalid_json")


def test_content_type_and_request_size_are_checked_before_analytical_io(
    authenticated_client, no_analytical_io
):
    client, _ = authenticated_client("analyst")
    denied(client.post(QUERY, "data", content_type="text/plain"), 415, "json_required")
    denied(
        client.post(QUERY, b'"' + b"x" * 33000 + b'"', content_type="application/json"),
        413,
        "request_too_large",
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/catalog?persona=admin",
        "/api/v1/data/fleet_daily?limit=1&limit=2",
        "/api/v1/data/fleet_daily?path=private.parquet",
        "/api/v1/auth/session?role=admin",
    ],
)
def test_unknown_or_duplicate_query_parameters_are_rejected_before_model_access(
    authenticated_client, no_analytical_io, path
):
    client, _ = authenticated_client("analyst")
    denied(client.get(path), 400, "invalid_parameter")


def test_unsupported_methods_do_not_create_mutations_or_run_queries(
    authenticated_client, no_analytical_io
):
    client, _ = authenticated_client("admin")
    denied(client.get(QUERY), 405, "method_not_allowed")
    denied(client.get("/api/v1/auth/logout"), 405, "method_not_allowed")
    response = client.delete("/api/v1/refresh", "{}", content_type="application/json")
    denied(response, 405, "method_not_allowed")
    assert response["Allow"] == "POST, GET"
    assert not RefreshJob.objects.exists()


def test_unknown_failures_and_sql_policy_errors_do_not_expose_sensitive_diagnostics(
    authenticated_client, monkeypatch, caplog
):
    client, _ = authenticated_client("analyst")
    canary = "secret-key-and-private-D-drive-canary"
    response = post(client, {**BODY, "sql": f"SELECT read_blob('{canary}') FROM fleet_daily"})
    denied(response, 422)
    assert canary not in response.content.decode() + caplog.text

    def broken_execution(*args):
        raise RuntimeError(f"D:/private/key.txt {canary} SELECT outage_mw FROM fleet_daily")

    monkeypatch.setattr(api_services, "_run", broken_execution)
    response = post(client, BODY)
    denied(response, 500, "internal_error")
    assert canary not in response.content.decode() + caplog.text
    assert "D:/private" not in response.content.decode()
    assert "SELECT" not in response.content.decode()


@pytest.mark.parametrize(
    "sandbox_code,status,api_code",
    [
        ("source_changed", 503, "data_integrity"),
        ("query_timeout", 504, "query_timeout"),
        ("result_too_large", 413, "result_too_large"),
        ("sandbox_unavailable", 503, "sandbox_unavailable"),
    ],
)
def test_execution_failures_use_safe_operational_error_contracts(
    authenticated_client, monkeypatch, sandbox_code, status, api_code
):
    client, _ = authenticated_client("analyst")

    def fail(*args, **kwargs):
        raise SandboxError(sandbox_code)

    monkeypatch.setattr(api_services, "run_query", fail)
    response = post(client, BODY)
    denied(response, status, api_code)
    assert not QueryCacheEntry.objects.exists()
