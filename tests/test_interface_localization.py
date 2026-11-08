"""Exercise server language negotiation without changing identity or analytical data."""

import json
import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory
from django.utils import translation

from outage_explorer.service.errors import APIError, problem
from outage_explorer.service.localization import catalog_presentation, public_message
from outage_explorer.service.models import RefreshJob


@pytest.fixture(autouse=True)
def isolated_language():
    """Restore thread-local language state after requests intentionally select Spanish."""
    with translation.override("en"):
        yield


def test_spanish_login_negotiates_headers_and_keeps_security_policy():
    reply = Client().get("/login/", HTTP_ACCEPT_LANGUAGE="es-MX,es;q=0.9,en;q=0.8")
    assert reply.status_code == 200
    html = reply.content.decode()
    assert '<html lang="es">' in html
    assert "Iniciar sesión" in html and "Contraseña" in html
    assert 'id="language-form"' in html and 'action="/language/"' in html
    assert "English" in html and "Español" in html
    assert reply["Content-Language"] == "es"
    assert {"Cookie", "Accept-Language"} <= set(reply["Vary"].split(", "))
    assert reply["Cache-Control"] == "no-store"
    assert "script-src 'self'" in reply["Content-Security-Policy"]
    assert "unsafe-inline" not in reply["Content-Security-Policy"]


def test_language_cookie_overrides_browser_preference_and_unsupported_languages_fall_back():
    client = Client()
    client.cookies["outage_language"] = "es"
    assert client.get("/login/", HTTP_ACCEPT_LANGUAGE="en")["Content-Language"] == "es"
    client.cookies["outage_language"] = "en"
    assert client.get("/login/", HTTP_ACCEPT_LANGUAGE="es")["Content-Language"] == "en"
    client.cookies["outage_language"] = "unsupported"
    assert client.get("/login/", HTTP_ACCEPT_LANGUAGE="fr")["Content-Language"] == "en"


@pytest.mark.parametrize("next_url", ["https://outside.invalid/collect", "//outside.invalid/"])
def test_language_switch_is_csrf_protected_and_cannot_redirect_off_origin(next_url):
    client = Client(enforce_csrf_checks=True)
    refused = client.post("/language/", {"language": "es", "next": "/#sql"})
    assert refused.status_code == 403
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/language/",
        {"language": "es", "next": next_url},
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 302 and response["Location"] == "/"
    cookie = response.cookies["outage_language"]
    assert cookie.value == "es" and cookie["samesite"] == "Lax"
    assert cookie["max-age"] == 365 * 24 * 60 * 60
    assert "sessionid" not in response.cookies
    assert client.get("/login/")["Content-Language"] == "es"


def test_language_switch_preserves_local_fragment_and_rejects_unsupported_or_get():
    client = Client()
    reply = client.post("/language/", {"language": "es", "next": "/#users"})
    assert reply.status_code == 302 and reply["Location"] == "/#users"
    rejected = client.post("/language/", {"language": "fr", "next": "/#users"})
    assert rejected.status_code == 400 and rejected.json()["code"] == "invalid_language"
    assert "outage_language" not in rejected.cookies
    assert client.get("/language/").status_code == 405


def test_spanish_private_shell_preserves_role_identity_and_user_supplied_text(
    authenticated_client,
):
    client, user = authenticated_client("admin")
    original = client.get("/api/v1/auth/session").json()
    # This username intentionally matches an authored English UI label. It must
    # remain user content rather than being translated into "Usuarios".
    get_user_model().objects.filter(pk=user.pk).update(username="Users")
    client.cookies["outage_language"] = "es"
    reply = client.get("/")
    html = reply.content.decode()
    assert reply.status_code == 200 and '<html lang="es">' in html
    assert "Crear usuario" in html and "Usuarios" in html
    assert '<span class="account-name">Users</span>' in html
    assert "SELECT period, offline_share, offline_pct" in html
    assert "FROM fleet_daily" in html and "ORDER BY period DESC" in html
    state = json.loads(
        re.search(r'<script id="frontend-session" type="application/json">(.*?)</script>', html)[1]
    )
    assert state["username"] == "Users" and state["persona"] == "admin"
    assert state["session_scope"] == original["session_scope"]
    assert client.get("/api/v1/auth/session").json()["session_scope"] == original["session_scope"]


def test_localized_api_errors_keep_machine_codes_and_dynamic_field_identifiers(
    authenticated_client,
):
    client, _ = authenticated_client("viewer")
    client.cookies["outage_language"] = "es"
    response = client.get("/api/v1/users")
    assert response.status_code == 403
    assert response.json()["code"] == "admin_required"
    assert response.json()["detail"] != "This capability requires an Admin persona."
    assert response.json()["title"] != "admin required"
    with translation.override("es"):
        detail = "start must be an ISO calendar date."
        localized = public_message(detail)
        assert localized == "start debe ser una fecha ISO válida."
        response = problem(RequestFactory().get("/"), APIError(400, "new_error_code", detail))
        payload = json.loads(response.content)
        assert payload["code"] == "new_error_code" and payload["detail"] == localized
        assert payload["title"] == "La solicitud falló"


def test_metadata_translation_does_not_mutate_catalog_keys_schema_or_values():
    catalog = {
        "model_id": "a" * 32,
        "datasets": [
            {
                "id": "facility_daily",
                "description": "Facility observations with observation-time names.",
                "schema": [["capacity_mw", "DECIMAL(24,9)"]],
                "rows": 438504,
                "units": {"capacity_mw": "MW"},
            }
        ],
        "warnings": [],
        "metrics": {},
    }
    snapshot = json.dumps(catalog, sort_keys=True)
    with translation.override("es"):
        result = catalog_presentation(catalog)
    assert result["datasets"][0]["description"] != catalog["datasets"][0]["description"]
    assert result["datasets"][0]["id"] == "facility_daily"
    assert result["datasets"][0]["schema"] == [["capacity_mw", "DECIMAL(24,9)"]]
    assert result["datasets"][0]["rows"] == 438504
    assert result["datasets"][0]["units"] == {"capacity_mw": "MW"}
    assert json.dumps(catalog, sort_keys=True) == snapshot


def test_refresh_message_localizes_at_read_time_without_rewriting_job_state(authenticated_client):
    client, user = authenticated_client("admin")
    job = RefreshJob.objects.create(
        owner=user, mode="recent", status="queued", public_message="Refresh is queued."
    )
    client.cookies["outage_language"] = "es"
    reply = client.get(f"/api/v1/refresh/{job.pk}")
    assert reply.status_code == 200
    assert reply.json()["status"] == "queued"
    assert reply.json()["message"] == "La actualización está en cola."
    job.refresh_from_db()
    assert job.public_message == "Refresh is queued."


def test_locale_neutral_assets_keep_identical_bytes_and_shared_etags():
    client = Client()
    first = client.get("/assets/app.css", HTTP_ACCEPT_LANGUAGE="en")
    client.cookies["outage_language"] = "es"
    second = client.get("/assets/app.css", HTTP_ACCEPT_LANGUAGE="es")
    assert first.content == second.content and first["ETag"] == second["ETag"]
    assert second["Cache-Control"] == "public, max-age=0, must-revalidate"
    assert "Vary" not in second and "Content-Language" not in second
