"""Exercise real-session Admin account management and permanent credential revocation."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, transaction
from django.db.models import F
from django.test import Client, RequestFactory
from django.utils.crypto import salted_hmac

from outage_explorer.data.storage import writer_lock
from outage_explorer.service import user_management
from outage_explorer.service.errors import APIError
from outage_explorer.service.models import Persona, QueryCacheEntry, RefreshJob, SavedView
from outage_explorer.service.policy import identity

from .conftest import API_PASSWORD

pytestmark = pytest.mark.django_db


def send(client, method, path, payload):
    return getattr(client, method)(path, json.dumps(payload), content_type="application/json")


def signin(username, password):
    """Authenticate through actual credential, CSRF and persisted-session transport."""
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/login",
        json.dumps({"username": username, "password": password}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    if response.status_code == 200:
        client.defaults["HTTP_X_CSRFTOKEN"] = response.json()["csrf_token"]
    return client, response


def row(client, user):
    response = client.get("/api/v1/users")
    assert response.status_code == 200, response.content
    return next(item for item in response.json()["users"] if item["id"] == user.pk)


def location(user):
    return f"/api/v1/users/{user.pk}"


def request_for(client, user):
    request = RequestFactory().post("/api/v1/users")
    request.user = user
    request.session = client.session
    return request


def test_create_hashes_one_time_password_without_infrastructure_privileges(authenticated_client):
    admin, _ = authenticated_client("admin")
    result = send(
        admin,
        "post",
        "/api/v1/users",
        {
            "username": "new.person",
            "first_name": "  Ada  ",
            "last_name": "Lovelace",
            "role": "analyst",
        },
    )
    assert result.status_code == 201, result.content
    payload = result.json()
    password = payload["generated_password"]
    assert len(password) == 32 and payload["reauthenticate"] is False
    assert result["Cache-Control"] == "no-store"
    user = get_user_model().objects.get(username="new.person")
    assert user.check_password(password) and user.password != password
    assert not user.is_staff and not user.is_superuser and user.is_active
    assert user.first_name == "Ada" and user.persona.role == "analyst"
    assert payload["user"]["version"] == row(admin, user)["version"]
    assert password not in admin.get("/api/v1/users").content.decode()
    assert "password" not in payload["user"]
    _, response = signin(user.username, password)
    assert response.status_code == 200


@pytest.mark.parametrize("role", ["viewer", "analyst", "analyst2"])
def test_non_admin_cannot_enumerate_or_mutate_accounts(authenticated_client, role):
    client, user = authenticated_client(role)
    assert client.get("/api/v1/users").status_code == 403
    for method, path, data in (
        ("post", "/api/v1/users", {"username": "escape", "role": "admin"}),
        ("put", location(user), {"version": "a" * 64, "role": "admin"}),
        ("delete", location(user), {"version": "a" * 64}),
        ("post", location(user) + "/password", {"version": "a" * 64}),
    ):
        assert send(client, method, path, data).status_code == 403


def test_anonymous_requests_and_missing_csrf_do_not_mutate(authenticated_client):
    anonymous = Client()
    assert anonymous.get("/api/v1/users").status_code == 401
    admin, user = authenticated_client("admin")
    admin.defaults.pop("HTTP_X_CSRFTOKEN")
    for method, path, data in (
        ("post", "/api/v1/users", {"username": "escape", "role": "viewer"}),
        ("patch", location(user), {"version": "a" * 64, "username": "escape"}),
        ("delete", location(user), {"version": "a" * 64}),
        ("post", location(user) + "/password", {"version": "a" * 64}),
    ):
        response = send(admin, method, path, data)
        assert response.status_code == 403 and response.json()["code"] == "csrf_failed"
    assert not get_user_model().objects.filter(username="escape").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"username": ""},
        {"username": "invalid space"},
        {"username": "x" * 151},
        {"username": "\ud800"},
        {"first_name": "x" * 151},
        {"last_name": "a\nb"},
        {"role": "superuser"},
        {"role": None},
        {"is_active": 1},
        {"is_staff": True},
        {"is_superuser": True},
        {"password": "Client-Chosen-Password-123!"},
    ],
)
def test_invalid_and_untrusted_create_fields_are_rejected(authenticated_client, changes):
    admin, _ = authenticated_client("admin")
    result = send(admin, "post", "/api/v1/users", {"username": "valid", "role": "viewer"} | changes)
    assert result.status_code == 400
    assert not get_user_model().objects.filter(username="valid").exists()


def test_usernames_are_reserved_case_insensitively_across_hidden_accounts(authenticated_client):
    admin, _ = authenticated_client("admin")
    for name in ("VIEWER", "admin"):
        result = send(admin, "post", "/api/v1/users", {"username": name, "role": "viewer"})
        assert result.status_code == 409 and result.json()["code"] == "username_unavailable"
    hidden = get_user_model().objects.create_user(username="infrastructure", is_staff=True)
    Persona.objects.create(user=hidden, role="admin")
    assert hidden.pk not in [item["id"] for item in admin.get("/api/v1/users").json()["users"]]
    result = send(admin, "post", "/api/v1/users", {"username": "infrastructure", "role": "viewer"})
    assert result.status_code == 409
    for method, path, payload in (
        ("put", location(hidden), {"version": "a" * 64, "username": "escape"}),
        ("delete", location(hidden), {"version": "a" * 64}),
        ("post", location(hidden) + "/password", {"version": "a" * 64}),
    ):
        assert send(admin, method, path, payload).status_code == 404


def test_rename_and_profile_edits_preserve_existing_user_session(authenticated_client):
    admin, _ = authenticated_client("admin")
    target, user = authenticated_client("analyst")
    original = target.get("/api/v1/auth/session").json()["session_scope"]
    current = row(admin, user)
    result = send(
        admin,
        "patch",
        location(user),
        {
            "version": current["version"],
            "username": "renamed",
            "first_name": "Grace",
            "last_name": "Hopper",
        },
    )
    assert result.status_code == 200, result.content
    assert result.json()["user"]["username"] == "renamed"
    assert result.json()["user"]["version"] != current["version"]
    session = target.get("/api/v1/auth/session").json()
    assert session["user"]["username"] == "renamed" and session["session_scope"] == original
    user.refresh_from_db()
    assert user.first_name == "Grace" and user.last_name == "Hopper"
    assert user.persona.session_version == 1


def test_self_rename_does_not_invalidate_session_or_binding(authenticated_client):
    admin, user = authenticated_client("admin")
    before = admin.get("/api/v1/auth/session").json()["session_scope"]
    response = send(
        admin,
        "put",
        location(user),
        {"version": row(admin, user)["version"], "username": "renamed.admin"},
    )
    assert response.status_code == 200
    assert admin.get("/api/v1/auth/session").json()["session_scope"] == before


def test_edit_versions_detect_stale_updates_resets_and_deletes(authenticated_client, api_users):
    admin, _ = authenticated_client("admin")
    user = api_users["viewer"]
    version = row(admin, user)["version"]
    changed = send(admin, "put", location(user), {"version": version, "first_name": "First editor"})
    assert changed.status_code == 200
    for method, path, payload in (
        ("put", location(user), {"version": version, "first_name": "Stale editor"}),
        ("delete", location(user), {"version": version}),
        ("post", location(user) + "/password", {"version": version}),
    ):
        response = send(admin, method, path, payload)
        assert response.status_code == 409 and response.json()["code"] == "user_changed"
        assert "generated_password" not in response.json()


@pytest.mark.parametrize(
    "field,changed,restored", [("role", "viewer", "analyst"), ("is_active", False, True)]
)
def test_access_changes_permanently_revoke_all_old_sessions_even_after_regrant(
    authenticated_client, field, changed, restored
):
    admin, _ = authenticated_client("admin")
    first, user = authenticated_client("analyst")
    second, _ = authenticated_client("analyst")
    response = send(
        admin, "put", location(user), {"version": row(admin, user)["version"], field: changed}
    )
    assert response.status_code == 200
    restored_response = send(
        admin,
        "put",
        location(user),
        {"version": response.json()["user"]["version"], field: restored},
    )
    assert restored_response.status_code == 200
    assert first.get("/api/v1/auth/session").status_code == 401
    assert second.get("/api/v1/auth/session").status_code == 401
    user.persona.refresh_from_db()
    assert user.persona.session_version == 3
    _, new_login = signin(user.username, API_PASSWORD)
    assert new_login.status_code == 200


def test_password_reset_revokes_old_sessions_and_returns_new_password_once(authenticated_client):
    admin, _ = authenticated_client("admin")
    target, user = authenticated_client("viewer")
    result = send(
        admin, "post", location(user) + "/password", {"version": row(admin, user)["version"]}
    )
    assert result.status_code == 200 and result["Cache-Control"] == "no-store"
    password = result.json()["generated_password"]
    assert len(password) == 32 and not result.json()["reauthenticate"]
    assert target.get("/api/v1/auth/session").status_code == 401
    user.refresh_from_db()
    assert user.check_password(password) and not user.check_password(API_PASSWORD)
    assert password not in admin.get("/api/v1/users").content.decode()
    _, denied = signin(user.username, API_PASSWORD)
    assert denied.status_code == 401
    _, permitted = signin(user.username, password)
    assert permitted.status_code == 200


def test_self_password_reset_delivers_password_then_requires_new_login(authenticated_client):
    admin, user = authenticated_client("admin")
    result = send(
        admin, "post", location(user) + "/password", {"version": row(admin, user)["version"]}
    )
    assert result.status_code == 200 and result.json()["reauthenticate"] is True
    password = result.json()["generated_password"]
    assert admin.get("/api/v1/users").status_code == 401
    replacement, response = signin(user.username, password)
    assert response.status_code == 200 and replacement.get("/api/v1/users").status_code == 200


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["create", "reset"])
def test_postcommit_admin_revocation_withholds_generated_credentials(
    authenticated_client, api_users, monkeypatch, operation
):
    """Committed target changes must not expose a password to a newly revoked actor."""
    admin, actor_user = authenticated_client("admin")
    target = api_users["viewer"]
    version = row(admin, target)["version"]

    def retire_actor_after_commit(actor, user_id, action):
        transaction.on_commit(
            lambda: Persona.objects.filter(user_id=actor.user_id).update(
                session_version=F("session_version") + 1
            )
        )

    monkeypatch.setattr(user_management, "_audit", retire_actor_after_commit)
    if operation == "create":
        response = send(
            admin, "post", "/api/v1/users", {"username": "committed.account", "role": "viewer"}
        )
        assert get_user_model().objects.filter(username="committed.account").exists()
    else:
        response = send(admin, "post", location(target) + "/password", {"version": version})
        target.refresh_from_db()
        assert not target.check_password(API_PASSWORD)
    assert response.status_code == 401
    assert "generated_password" not in response.json()
    actor_user.persona.refresh_from_db()
    assert actor_user.persona.session_version == 2


@pytest.mark.parametrize("change", [{"role": "viewer"}, {"is_active": False}, None])
def test_last_admin_cannot_be_removed(authenticated_client, change):
    admin, user = authenticated_client("admin")
    data = {"version": row(admin, user)["version"]}
    method = "delete" if change is None else "put"
    result = send(admin, method, location(user), data | (change or {}))
    assert result.status_code == 409 and result.json()["code"] == "last_admin"
    assert admin.get("/api/v1/users").status_code == 200


@pytest.mark.parametrize("change", [{"role": "viewer"}, {"is_active": False}, None])
def test_self_lockout_is_rejected_even_when_another_admin_exists(authenticated_client, change):
    admin, user = authenticated_client("admin")
    other = get_user_model().objects.create_user(username="another.admin")
    Persona.objects.create(user=other, role="admin")
    data = {"version": row(admin, user)["version"]}
    result = send(
        admin, "delete" if change is None else "put", location(user), data | (change or {})
    )
    assert result.status_code == 409 and result.json()["code"] == "self_lockout"


def test_delete_retains_completed_refresh_history_and_removes_owned_saved_content(
    authenticated_client, api_users
):
    admin, _ = authenticated_client("admin")
    user = api_users["analyst"]
    job = RefreshJob.objects.create(owner=user, mode="recent", status="succeeded")
    SavedView.objects.create(
        owner=user,
        name="Owned",
        dataset_id="fleet_daily",
        sql="SELECT * FROM fleet_daily",
        created_model_id="a" * 32,
    )
    QueryCacheEntry.objects.create(
        owner=user,
        key="a" * 64,
        role="analyst",
        model_id="a" * 32,
        publication_id="a" * 32,
        expires_at=job.created_at,
        payload={},
    )
    result = send(admin, "delete", location(user), {"version": row(admin, user)["version"]})
    assert result.status_code == 200 and result.json() == {"deleted": True}
    assert not get_user_model().objects.filter(pk=user.pk).exists()
    job.refresh_from_db()
    assert job.owner_id is None and job.status == "succeeded"
    assert not SavedView.objects.exists() and not QueryCacheEntry.objects.exists()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_delete_cannot_detach_an_active_refresh_owner(authenticated_client, api_users, status):
    admin, _ = authenticated_client("admin")
    user = api_users["analyst"]
    job = RefreshJob.objects.create(owner=user, mode="recent", status=status)
    response = send(admin, "delete", location(user), {"version": row(admin, user)["version"]})
    assert response.status_code == 409 and response.json()["code"] == "user_has_active_refresh"
    job.refresh_from_db()
    assert job.owner_id == user.pk


def test_bounded_search_keyset_pages_and_cursors_are_bound_to_admin_and_filters(
    authenticated_client,
):
    admin, _ = authenticated_client("admin")
    page = admin.get("/api/v1/users", {"page_size": 2}).json()
    assert len(page["users"]) == 2 and page["next_cursor"]
    next_page = admin.get("/api/v1/users", {"page_size": 2, "cursor": page["next_cursor"]})
    assert next_page.status_code == 200 and len(next_page.json()["users"]) == 2
    assert not (
        {item["id"] for item in page["users"]} & {item["id"] for item in next_page.json()["users"]}
    )
    assert next_page.json()["next_cursor"] is None
    assert len(admin.get("/api/v1/users", {"search": "ANALYST"}).json()["users"]) == 2
    assert admin.get("/api/v1/users", {"search": "%"}).json()["users"] == []
    for arguments in (
        {"page_size": 3, "cursor": page["next_cursor"]},
        {"page_size": 2, "search": "admin", "cursor": page["next_cursor"]},
        {"page_size": 2, "cursor": page["next_cursor"] + "bad"},
        {"page_size": 0},
        {"page_size": 101},
        {"search": "x" * 151},
        {"search": "\x00"},
    ):
        assert admin.get("/api/v1/users", arguments).status_code == 400
    second, _ = authenticated_client("admin")
    assert (
        second.get("/api/v1/users", {"page_size": 2, "cursor": page["next_cursor"]}).status_code
        == 400
    )


def test_session_version_one_keeps_legacy_binding_and_missing_epoch_session(authenticated_client):
    admin, user = authenticated_client("admin")
    session = admin.session
    session.pop("_outage_session_version")
    session.save()
    request = request_for(admin, user)
    actor = identity(request)
    expected = salted_hmac(
        "outage-actor", f"{user.pk}:admin:{session.session_key}:{session['_auth_user_hash']}"
    ).hexdigest()
    assert actor.binding == expected
    assert admin.get("/api/v1/auth/session").status_code == 200


def test_persisted_epoch_disagreement_revokes_a_cached_request(authenticated_client):
    admin, user = authenticated_client("admin")
    request = request_for(admin, user)
    session = admin.session
    session["_outage_session_version"] = 2
    session.save()
    with pytest.raises(APIError, match="Sign in again"):
        identity(request)


def test_busy_account_lock_returns_safe_retry_and_rechecks_revoked_actor(
    authenticated_client, settings, monkeypatch
):
    admin, user = authenticated_client("admin")
    with writer_lock(settings.OUTAGE_STATE_DIR / "user-management"):
        response = send(admin, "post", "/api/v1/users", {"username": "blocked", "role": "viewer"})
    assert response.status_code == 503 and response["Retry-After"] == "1"
    request = request_for(admin, user)
    actor = identity(request)
    Persona.objects.filter(user=user).update(role="viewer")
    with pytest.raises(APIError):
        user_management.create(request, actor, {"username": "revoked", "role": "viewer"})
    assert not get_user_model().objects.filter(username__in=["blocked", "revoked"]).exists()


@pytest.mark.parametrize("identifier", [0, 2**63, 10**100])
def test_oversized_account_ids_are_rejected_without_sqlite_overflow(
    authenticated_client, identifier
):
    admin, _ = authenticated_client("admin")
    response = send(admin, "delete", f"/api/v1/users/{identifier}", {"version": "a" * 64})
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_competing_admin_demotions_leave_one_active_admin(authenticated_client):
    first, first_user = authenticated_client("admin")
    second_user = get_user_model().objects.create_user(
        username="second-admin", password=API_PASSWORD
    )
    Persona.objects.create(user=second_user, role="admin")
    second, response = signin(second_user.username, API_PASSWORD)
    assert response.status_code == 200
    versions = {item["id"]: item["version"] for item in first.get("/api/v1/users").json()["users"]}
    ready = Barrier(2)

    def demote(client, target):
        close_old_connections()
        try:
            ready.wait(timeout=5)
            return send(
                client, "put", location(target), {"version": versions[target.pk], "role": "viewer"}
            ).status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        calls = [
            executor.submit(demote, first, second_user),
            executor.submit(demote, second, first_user),
        ]
        statuses = [call.result(timeout=15) for call in calls]
    assert statuses.count(200) == 1 and all(value in {200, 401, 403, 503} for value in statuses)
    assert get_user_model().objects.filter(is_active=True, persona__role="admin").count() == 1
