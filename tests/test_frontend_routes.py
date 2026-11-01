"""Private browser shells and public, fixed assets preserve API boundaries."""

from __future__ import annotations

import json
import re
import shutil
from html.parser import HTMLParser

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import Client

from outage_explorer.frontend import views
from outage_explorer.pipeline import IngestConfig, ingest
from outage_explorer.service import api_services
from outage_explorer.service.models import Persona
from outage_explorer.storage import Catalog

from .helpers import FakeClient

pytestmark = pytest.mark.django_db


class SessionScript(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.active = False
        self.parts = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "frontend-session":
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def value(self):
        return json.loads("".join(self.parts))


@pytest.fixture
def no_analytical_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("HTML routes must not read analytical data or execute a query")

    for name in ("catalog", "preview", "query", "_bundle", "_run"):
        monkeypatch.setattr(api_services, name, forbidden)
    monkeypatch.setattr(Catalog, "__init__", forbidden)


def test_anonymous_home_uses_fixed_login_redirect(no_analytical_access):
    response = Client().get("/?next=https://evil.invalid/collect")
    assert response.status_code == 302
    assert response["Location"] == "/login/"
    assert response["Cache-Control"] == "no-store"


def test_public_login_ignores_external_return_urls_and_contains_no_credentials(
    no_analytical_access,
):
    response = Client().get("/login/?next=https://evil.invalid/collect")
    assert response.status_code == 200
    html = response.content.decode()
    assert "evil.invalid" not in html
    assert "Synthetic-API-Test-Password" not in html
    assert "frontend-session" not in html
    assert response["Cache-Control"] == "no-store"
    csp = response["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp
    assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp


@pytest.mark.parametrize("role", ["viewer", "analyst", "admin"])
def test_authenticated_shell_exposes_only_fresh_role_and_opaque_session_scope(
    authenticated_client, no_analytical_access, role
):
    client, _ = authenticated_client(role)
    response = client.get("/")
    assert response.status_code == 200, response.content
    html = response.content.decode()
    state = SessionScript(html).value()
    assert set(state) == {"username", "persona", "session_scope"}
    assert state["username"] == role and state["persona"] == role
    assert re.fullmatch(r"[0-9a-f]{64}", state["session_scope"])
    current = client.get("/api/v1/auth/session")
    assert current.status_code == 200
    assert current.json()["session_scope"] == state["session_scope"]
    assert client.session.session_key not in current.content.decode()
    assert client.session.session_key not in html
    assert ('id="admin-panel"' in html) is (role == "admin")
    assert response["Cache-Control"] == "no-store"
    assert response["X-Content-Type-Options"] == "nosniff"


def test_valid_identity_on_login_redirects_only_to_home(authenticated_client, no_analytical_access):
    client, _ = authenticated_client("viewer")
    response = client.get("/login/?next=//evil.invalid/")
    assert response.status_code == 302 and response["Location"] == "/"


def test_persona_downgrade_removes_admin_html_and_rotates_history_scope(
    authenticated_client, no_analytical_access
):
    client, user = authenticated_client("admin")
    before = client.get("/")
    assert b'id="admin-panel"' in before.content
    original_scope = SessionScript(before.content.decode()).value()["session_scope"]
    Persona.objects.filter(user=user).update(role="viewer")
    after = client.get("/")
    assert after.status_code == 200
    assert b'id="admin-panel"' not in after.content
    current = SessionScript(after.content.decode()).value()
    assert current["persona"] == "viewer" and current["session_scope"] != original_scope
    assert client.get("/api/v1/auth/session").json()["session_scope"] == current["session_scope"]


def test_new_server_session_for_same_account_has_a_different_browser_scope(
    authenticated_client, no_analytical_access
):
    first, _ = authenticated_client("viewer")
    second, _ = authenticated_client("viewer")
    assert first.session.session_key != second.session.session_key
    original = first.get("/api/v1/auth/session").json()["session_scope"]
    replacement = second.get("/api/v1/auth/session").json()["session_scope"]
    assert replacement != original
    assert SessionScript(second.get("/").content.decode()).value()["session_scope"] == replacement
    assert first.get("/api/v1/auth/session").json()["session_scope"] == original


def test_publishing_a_new_data_model_does_not_rotate_browser_session_scope(
    authenticated_client, api_context, api_data_dir, tmp_path
):
    target = tmp_path / "scope-refresh-source"
    shutil.copytree(api_data_dir, target)
    api_context.OUTAGE_DATA_DIR = target
    client, _ = authenticated_client("viewer")
    original = client.get("/api/v1/auth/session").json()["session_scope"]
    with Catalog(target) as catalog:
        previous_model = catalog.current_model().id
    ingest(IngestConfig(target, "recent"), FakeClient())
    with Catalog(target) as catalog:
        assert catalog.current_model().id != previous_model
    assert client.get("/api/v1/auth/session").json()["session_scope"] == original
    assert SessionScript(client.get("/").content.decode()).value()["session_scope"] == original


@pytest.mark.parametrize("change", ["session_deleted", "inactive", "missing_persona"])
def test_revoked_identity_cannot_receive_private_html(
    authenticated_client, no_analytical_access, change
):
    client, user = authenticated_client("admin")
    if change == "session_deleted":
        Session.objects.filter(session_key=client.session.session_key).delete()
    elif change == "inactive":
        get_user_model().objects.filter(pk=user.pk).update(is_active=False)
    else:
        Persona.objects.filter(user=user).delete()
    response = client.get("/")
    assert response.status_code == 302 and response["Location"] == "/login/"
    login = client.get("/login/")
    assert login.status_code == 200 and b'"persona": "admin"' not in login.content


def test_username_is_fresh_and_escaped_in_html_and_json(authenticated_client, no_analytical_access):
    client, user = authenticated_client("viewer")
    username = '<img src=x onerror="alert(1)">'
    get_user_model().objects.filter(pk=user.pk).update(username=username)
    response = client.get("/")
    assert response.status_code == 200
    html = response.content.decode()
    assert username not in html
    assert SessionScript(html).value()["username"] == username
    assert "\\u003Cimg" in html


@pytest.mark.parametrize("change", ["logout", "downgrade"])
def test_identity_changes_during_render_never_deliver_old_admin_html(
    authenticated_client, no_analytical_access, monkeypatch, change
):
    client, user = authenticated_client("admin")
    original_render = views.render

    def render_then_revoke(*args, **kwargs):
        response = original_render(*args, **kwargs)
        assert b'id="admin-panel"' in response.content
        if change == "logout":
            Session.objects.filter(session_key=client.session.session_key).delete()
        else:
            Persona.objects.filter(user=user).update(role="viewer")
        return response

    monkeypatch.setattr(views, "render", render_then_revoke)
    response = client.get("/")
    assert response.status_code == 302 and response["Location"] == "/login/"
    assert b'id="admin-panel"' not in response.content


@pytest.mark.parametrize("path", ["/", "/login/"])
def test_html_routes_accept_get_only(no_analytical_access, path):
    response = Client().post(path, "{}", content_type="application/json")
    assert response.status_code == 405 and response["Allow"] == "GET"


@pytest.mark.parametrize(
    "filename",
    [
        "app.css",
        "app.js",
        "login.js",
        "api.js",
        "dom.js",
        "table.js",
        "chart.js",
        "sql.js",
        "refresh.js",
        "overview.js",
        "explore.js",
        "favicon.svg",
    ],
)
def test_packaged_assets_work_without_debug_and_revalidate_with_etag(settings, filename):
    settings.DEBUG = False
    client = Client()
    response = client.get(f"/assets/{filename}")
    assert response.status_code == 200, response.content
    assert response.content
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cache-Control"] == "public, max-age=0, must-revalidate"
    assert not response.cookies
    matching = client.get(f"/assets/{filename}", HTTP_IF_NONE_MATCH=response["ETag"])
    assert matching.status_code == 304 and not matching.content
    assert matching["ETag"] == response["ETag"]
    alias = client.get(f"/assets/outage_explorer/{filename}")
    assert alias.status_code == 200 and alias.content == response.content


@pytest.mark.parametrize(
    "path",
    [
        "/assets/key.txt",
        "/assets/local-config.json",
        "/assets/app.js.map",
        "/assets/../key.txt",
        "/assets/%2e%2e%2fkey.txt",
        "/assets/..%5ckey.txt",
        "/assets/outage_explorer/../../key.txt",
        "/assets/app.js.",
    ],
)
def test_asset_allowlist_rejects_private_names_and_traversal_before_resource_lookup(
    monkeypatch, path
):
    monkeypatch.setattr(
        views, "files", lambda *args: pytest.fail("Do not look up non-allowlisted resources")
    )
    response = Client().get(path)
    assert response.status_code == 404
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cache-Control"] == "no-store"


def test_api_response_policy_remains_json_only_and_private():
    response = Client().get("/api/v1/health/live")
    assert response.status_code == 200 and response["Content-Type"] == "application/json"
    assert response["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert response["Cache-Control"] == "no-store"
