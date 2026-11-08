"""Supply isolated saved data and real-session HTTP fixtures for API tests.

Synthetic source rows build one session-scoped model; per-test application state and
pytest-django's database replace the operator's .service/data directories. The password
constant is synthetic fixture data, never an operator credential.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from outage_explorer.data.pipeline import IngestConfig, ingest

from .helpers import FakeClient

API_PASSWORD = "Synthetic-API-Test-Password-123!"


@pytest.fixture(scope="session")
def api_data_dir(tmp_path_factory):
    """Publish a reusable model under pytest's temporary root using FakeClient only."""
    path = tmp_path_factory.mktemp("api-synthetic-data")
    ingest(IngestConfig(path, "full"), FakeClient())
    return path


@pytest.fixture
def api_context(db, settings, api_data_dir, tmp_path):
    """Bind Django test settings to synthetic analytics and per-test service state."""
    settings.OUTAGE_DATA_DIR = api_data_dir
    settings.OUTAGE_STATE_DIR = tmp_path / "service-state"
    settings.OUTAGE_STATE_DIR.mkdir()
    return settings


@pytest.fixture
def api_users(api_context):
    """Create role fixtures, including a second Analyst for isolation/concurrency cases."""
    from outage_explorer.service.models import Persona

    users = {}
    for username in ("viewer", "analyst", "admin", "analyst2"):
        user = get_user_model().objects.create_user(username=username, password=API_PASSWORD)
        Persona.objects.create(user=user, role="analyst" if username == "analyst2" else username)
        users[username] = user
    return users


@pytest.fixture
def authenticated_client(api_users):
    """Return a factory that performs the real CSRF/session login flow."""

    def make(role):
        """Sign in as a synthetic role and retain the rotated CSRF token on its client.

        Close over api_users so tests receive the matching persisted user and a browser-like
        Client with CSRF enforcement; no forced authentication bypass is used.
        """
        client = Client(enforce_csrf_checks=True)
        token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        result = client.post(
            "/api/v1/auth/login",
            json.dumps({"username": role, "password": API_PASSWORD}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        assert result.status_code == 200, result.content
        client.defaults["HTTP_X_CSRFTOKEN"] = result.json()["csrf_token"]
        return client, api_users[role]

    return make
