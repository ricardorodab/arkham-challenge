"""An in-flight request must observe persisted session revocation before delivery."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.test import Client
from django.utils import timezone

from outage_explorer.service import api_services
from outage_explorer.service.models import QueryCacheEntry

pytestmark = pytest.mark.django_db
BODY = {"dataset_id": "fleet_daily", "sql": "SELECT period FROM fleet_daily"}
RESULT = {"columns": [{"name": "period", "type": "DATE"}], "rows": [["2026-09-01"]]}


def query(client):
    return client.post("/api/v1/query", json.dumps(BODY), content_type="application/json")


def revoked(response):
    assert response.status_code == 401, response.content
    assert response.json()["code"] == "session_revoked"
    assert not ({"rows", "columns", "model_id", "source_snapshot_id"} & response.json().keys())
    assert not QueryCacheEntry.objects.exists()


@pytest.mark.parametrize("operation", ["delete", "expire", "logout"])
def test_revocation_during_execution_prevents_result_and_cache_delivery(
    authenticated_client, monkeypatch, operation
):
    client, _ = authenticated_client("analyst")
    session_key = client.session.session_key

    def execute_then_revoke(*args):
        if operation == "delete":
            Session.objects.filter(session_key=session_key).delete()
        elif operation == "expire":
            Session.objects.filter(session_key=session_key).update(
                expire_date=timezone.now() - timedelta(seconds=1)
            )
        else:
            # Model a second tab logging out the same persisted browser session
            # after the query request has already resolved/cached its identity.
            other_tab = Client(enforce_csrf_checks=True)
            for name in (settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME):
                other_tab.cookies[name] = client.cookies[name].value
            response = other_tab.post(
                "/api/v1/auth/logout",
                "{}",
                content_type="application/json",
                HTTP_X_CSRFTOKEN=client.defaults["HTTP_X_CSRFTOKEN"],
            )
            assert response.status_code == 200, response.content
            assert not Session.objects.filter(session_key=session_key).exists()
        return RESULT

    monkeypatch.setattr(api_services, "_run", execute_then_revoke)
    revoked(query(client))


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("_auth_user_id", "99999999"),
        ("_auth_user_backend", "django.contrib.auth.backends.RemoteUserBackend"),
        ("_auth_user_hash", "changed-after-request-authentication"),
    ],
)
def test_persisted_auth_binding_changes_during_execution_are_rejected(
    authenticated_client, monkeypatch, field, replacement
):
    client, _ = authenticated_client("analyst")
    session_key = client.session.session_key

    def execute_then_replace_auth_binding(*args):
        record = Session.objects.get(session_key=session_key)
        payload = record.get_decoded()
        payload[field] = replacement
        record.session_data = SessionStore().encode(payload)
        record.save(update_fields=["session_data"])
        return RESULT

    monkeypatch.setattr(api_services, "_run", execute_then_replace_auth_binding)
    revoked(query(client))


def test_revoked_session_is_rejected_before_cached_results_are_read(
    authenticated_client, monkeypatch
):
    client, _ = authenticated_client("analyst")
    monkeypatch.setattr(api_services, "_run", lambda *args: RESULT)
    assert query(client).status_code == 200
    assert QueryCacheEntry.objects.exists()
    Session.objects.filter(session_key=client.session.session_key).delete()

    def no_data_or_cache_access(*args):
        pytest.fail("A revoked session must be denied before query/cache/model access")

    monkeypatch.setattr(api_services, "query", no_data_or_cache_access)
    response = query(client)
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
