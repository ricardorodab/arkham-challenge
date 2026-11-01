"""Reject malformed credentials and snapshot selectors before privileged work."""

from __future__ import annotations

import json

import pytest

from outage_explorer.service import api_services, views

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("field", ["username", "password"])
def test_non_scalar_unicode_credentials_are_rejected_before_authentication(
    authenticated_client, monkeypatch, field
):
    client, _ = authenticated_client("analyst")

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid Unicode must be rejected before throttle/authentication I/O")

    monkeypatch.setattr(views, "limit_login", forbidden)
    monkeypatch.setattr(views, "authenticate", forbidden)
    payload = {"username": "analyst", "password": "Synthetic-Password-123!", field: "\ud800"}
    response = client.post(
        "/api/v1/auth/login", json.dumps(payload), content_type="application/json"
    )
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "invalid_parameter"
    assert "rows" not in response.json()


@pytest.mark.parametrize("model_id", ["", False, 0, []])
def test_falsy_invalid_model_selector_never_falls_back_to_current_publication(
    authenticated_client, monkeypatch, model_id
):
    client, _ = authenticated_client("analyst")

    def forbidden(*args, **kwargs):
        pytest.fail("Malformed model selectors must be rejected before model/query I/O")

    monkeypatch.setattr(api_services, "resolve_model", forbidden)
    monkeypatch.setattr(api_services, "_run", forbidden)
    response = client.post(
        "/api/v1/query",
        json.dumps(
            {
                "dataset_id": "fleet_daily",
                "sql": "SELECT period FROM fleet_daily",
                "model_id": model_id,
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "invalid_model_id"
    assert "model_id" not in response.json()


@pytest.mark.parametrize("path", ["/api/v1/catalog", "/api/v1/data/fleet_daily"])
def test_empty_model_selector_is_also_rejected_by_get_capabilities(
    authenticated_client, monkeypatch, path
):
    client, _ = authenticated_client("viewer")

    def forbidden(*args, **kwargs):
        pytest.fail("Malformed model selectors must not open the current model")

    monkeypatch.setattr(api_services, "resolve_model", forbidden)
    response = client.get(path, {"model_id": ""})
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "invalid_model_id"
