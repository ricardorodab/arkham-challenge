"""Exercise saved-definition persistence, viewer execution and audience/ownership boundaries."""

from __future__ import annotations

import json
import shutil

import pytest
from django.contrib.sessions.models import Session
from django.db import OperationalError

from outage_explorer.data.pipeline import IngestConfig, ingest
from outage_explorer.data.storage import writer_lock
from outage_explorer.service import api_services, saved_views
from outage_explorer.service.errors import APIError
from outage_explorer.service.models import Persona, SavedView

from .helpers import FakeClient

pytestmark = pytest.mark.django_db


def send(client, method, path, payload):
    """Use real JSON/CSRF transport for one saved-view operation."""
    return getattr(client, method)(path, json.dumps(payload), content_type="application/json")


def definition(client, **changes):
    """Build a valid national definition using this account's adopted synthetic model."""
    value = {
        "name": "Daily fleet output",
        "dataset_id": "fleet_daily",
        "sql": "SELECT period, offline_pct FROM fleet_daily ORDER BY period",
        "max_rows": 100,
        "chart": {"enabled": False},
        "shared_roles": [],
        "model_id": client.get("/api/v1/catalog").json()["model_id"],
    }
    return value | changes


def create(client, **changes):
    """Save one actual compiled definition and return its public envelope."""
    result = send(client, "post", "/api/v1/saved-views", definition(client, **changes))
    assert result.status_code == 201, result.content
    return result.json()["view"]


def location(view):
    return f"/api/v1/saved-views/{view['id']}"


def run(client, view, **changes):
    return send(
        client,
        "post",
        location(view) + "/run",
        {
            "revision": view["revision"],
            "model_id": client.get("/api/v1/catalog").json()["model_id"],
        }
        | changes,
    )


def test_definition_persists_without_rows_and_history_survives_a_new_session(authenticated_client):
    client, user = authenticated_client("analyst")
    view = create(client)
    assert view["is_owner"] and view["can_edit"] and view["can_delete"]
    assert "owner" not in view and "owner_id" not in view and "username" not in view
    stored = SavedView.objects.get(pk=view["id"])
    assert stored.owner == user and stored.sql == view["sql"]
    assert not ({"rows", "payload", "result"} & {field.name for field in SavedView._meta.fields})
    replacement, _ = authenticated_client("analyst")
    assert replacement.get(location(view)).json()["view"] == view
    listed = replacement.get("/api/v1/saved-views").json()
    assert listed["views"] == [view]
    assert listed["can_create"] and listed["can_share"] and listed["max_owned"] == 50


def test_private_definitions_are_hidden_from_other_users_and_admins(authenticated_client):
    owner, _ = authenticated_client("analyst")
    view = create(owner)
    for role in ("analyst2", "admin", "viewer"):
        client, _ = authenticated_client(role)
        assert client.get("/api/v1/saved-views").json()["views"] == []
        assert client.get(location(view)).status_code == 404
        assert run(client, view).status_code == 404
        assert send(client, "delete", location(view), {"revision": 1}).status_code == 404


def test_viewer_consumes_explicit_shared_national_view_but_cannot_author(authenticated_client):
    owner, _ = authenticated_client("analyst")
    view = create(owner, shared_roles=["viewer"])
    viewer, _ = authenticated_client("viewer")
    listed = viewer.get("/api/v1/saved-views").json()
    assert len(listed["views"]) == 1 and not listed["can_create"] and not listed["can_share"]
    assert not listed["views"][0]["is_owner"] and not listed["views"][0]["can_edit"]
    assert not listed["views"][0]["can_delete"]
    assert send(viewer, "post", "/api/v1/saved-views", definition(viewer)).status_code == 403
    assert send(viewer, "put", location(view), definition(viewer, revision=1)).status_code == 403
    result = run(viewer, view, page_size=100)
    assert result.status_code == 200, result.content
    packet = result.json()["result"]
    assert packet["dataset_id"] == "fleet_daily" and packet["rows"]
    assert packet["model_id"] == view["created_model_id"]


def test_viewer_never_receives_detail_definitions_even_if_audience_database_is_corrupt(
    authenticated_client,
):
    owner, _ = authenticated_client("analyst")
    view = create(owner, dataset_id="generator_daily", sql="SELECT * FROM generator_daily")
    SavedView.objects.filter(pk=view["id"]).update(share_viewer=True)
    viewer, _ = authenticated_client("viewer")
    assert viewer.get("/api/v1/saved-views").json()["views"] == []
    assert viewer.get(location(view)).status_code == 404
    assert run(viewer, view).status_code == 404


def test_cannot_publish_detail_data_to_viewers(authenticated_client):
    owner, _ = authenticated_client("analyst")
    payload = definition(
        owner,
        dataset_id="generator_daily",
        sql="SELECT * FROM generator_daily",
        shared_roles=["viewer"],
    )
    response = send(owner, "post", "/api/v1/saved-views", payload)
    assert response.status_code == 400 and response.json()["code"] == "invalid_audience"
    assert SavedView.objects.count() == 0


def test_ownership_and_revision_protect_update_delete_and_unsharing(authenticated_client):
    owner, _ = authenticated_client("analyst")
    view = create(owner, shared_roles=["analyst", "viewer"])
    other, _ = authenticated_client("analyst2")
    assert send(other, "put", location(view), definition(other, revision=1)).status_code == 404
    assert send(other, "delete", location(view), {"revision": 1}).status_code == 404
    edited = send(owner, "put", location(view), definition(owner, name="Renamed", revision=1))
    assert edited.status_code == 200, edited.content
    updated = edited.json()["view"]
    assert updated["name"] == "Renamed" and updated["revision"] == 2
    assert updated["shared_roles"] == []
    assert other.get(location(view)).status_code == 404
    assert send(owner, "put", location(view), definition(owner, revision=1)).status_code == 409
    assert send(owner, "delete", location(view), {"revision": 1}).status_code == 409
    assert run(owner, view).status_code == 409
    assert send(owner, "delete", location(view), {"revision": 2}).json() == {"deleted": True}
    assert owner.get(location(view)).status_code == 404


def test_downgraded_owner_can_remove_but_not_edit_its_permitted_existing_definition(
    authenticated_client,
):
    owner, user = authenticated_client("analyst")
    view = create(owner)
    Persona.objects.filter(user=user).update(role="viewer")
    visible = owner.get(location(view)).json()["view"]
    assert visible["is_owner"] and visible["can_delete"] and not visible["can_edit"]
    assert send(owner, "put", location(view), definition(owner, revision=1)).status_code == 403
    assert send(owner, "delete", location(view), {"revision": 1}).status_code == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"sql": "DELETE FROM fleet_daily"},
        {"sql": "SELECT * FROM generator_daily"},
        {"name": "\n"},
        {"name": "x" * 101},
        {"max_rows": 1001},
        {"max_rows": True},
        {"shared_roles": ["viewer", "viewer"]},
        {"shared_roles": ["unknown"]},
        {"model_id": "../private"},
        {"chart": {"enabled": "true"}},
        {"chart": {"enabled": True, "date_column": "period", "value_column": "period"}},
    ],
)
def test_invalid_metadata_or_sql_is_never_saved(authenticated_client, changes):
    owner, _ = authenticated_client("analyst")
    response = send(owner, "post", "/api/v1/saved-views", definition(owner, **changes))
    assert response.status_code in (400, 403, 422), response.content
    assert not SavedView.objects.exists()


@pytest.mark.parametrize(
    "chart",
    [
        {"enabled": True, "date_column": "missing", "value_column": "offline_pct"},
        {"enabled": True, "date_column": "period", "value_column": "period"},
        {"enabled": True, "date_column": "offline_pct", "value_column": "capacity_mw"},
        {
            "enabled": True,
            "date_column": "period",
            "value_column": "offline_pct",
            "series_column": "missing",
        },
    ],
)
def test_chart_fields_must_match_actual_executed_output_types(authenticated_client, chart):
    owner, _ = authenticated_client("analyst")
    response = send(owner, "post", "/api/v1/saved-views", definition(owner, chart=chart))
    assert response.status_code == 400 and response.json()["code"] == "invalid_chart"
    assert not SavedView.objects.exists()


def test_temporal_series_cannot_be_persisted_from_actual_query_output(authenticated_client):
    """Keep the server contract aligned with chartOptions' non-temporal series choices."""
    owner, _ = authenticated_client("analyst")
    payload = definition(
        owner,
        sql="SELECT period AS day, period AS another_day, offline_pct AS ratio FROM fleet_daily",
        chart={
            "enabled": True,
            "date_column": "day",
            "value_column": "ratio",
            "series_column": "another_day",
        },
    )
    response = send(owner, "post", "/api/v1/saved-views", payload)
    assert response.status_code == 400 and response.json()["code"] == "invalid_chart"
    assert not SavedView.objects.exists()


@pytest.mark.parametrize(
    "series_type", ["DATE", "TIMESTAMP", "TIMESTAMP_NS", "TIMESTAMP WITH TIME ZONE"]
)
def test_temporal_series_types_are_rejected_on_execution_schema_validation(series_type):
    """Reject temporal grouping labels even when a stored configuration predates validation."""
    with pytest.raises(APIError, match="non-temporal"):
        saved_views.validate_chart(
            {
                "enabled": True,
                "date_column": "day",
                "value_column": "value",
                "series_column": "series",
            },
            {
                "columns": [
                    {"name": "day", "type": "DATE"},
                    {"name": "value", "type": "INTEGER"},
                    {"name": "series", "type": series_type},
                ]
            },
        )


def test_chart_aliases_work_and_results_are_run_with_the_readers_identity(
    authenticated_client, monkeypatch
):
    owner, _ = authenticated_client("analyst")
    view = create(
        owner,
        sql="SELECT period AS day, offline_pct AS ratio FROM fleet_daily ORDER BY period",
        chart={"enabled": True, "date_column": "day", "value_column": "ratio"},
        shared_roles=["viewer"],
    )
    viewer, user = authenticated_client("viewer")
    original = api_services.query
    actors = []

    def capture(request, actor, payload):
        actors.append((actor.user_id, actor.role))
        return original(request, actor, payload)

    monkeypatch.setattr(api_services, "query", capture)
    response = run(viewer, view)
    assert response.status_code == 200, response.content
    assert actors == [(user.pk, "viewer")]
    assert response.json()["view"]["chart"] == {
        "enabled": True,
        "date_column": "day",
        "value_column": "ratio",
        "series_column": None,
    }


def test_run_refuses_unsharing_or_revision_changes_during_execution(
    authenticated_client, monkeypatch
):
    owner, _ = authenticated_client("analyst")
    view = create(owner, shared_roles=["viewer"])
    viewer, _ = authenticated_client("viewer")
    original = api_services.query

    def revoke(request, actor, payload):
        result = original(request, actor, payload)
        SavedView.objects.filter(pk=view["id"]).update(share_viewer=False, revision=2)
        return result

    monkeypatch.setattr(api_services, "query", revoke)
    response = run(viewer, view)
    assert response.status_code == 404
    assert "rows" not in response.json()


def test_session_revocation_during_create_rolls_back_definition(authenticated_client, monkeypatch):
    owner, _ = authenticated_client("analyst")
    original = saved_views._validated

    def revoke(request, actor, definition, model_id):
        original(request, actor, definition, model_id)
        Session.objects.filter(session_key=request.session.session_key).delete()

    monkeypatch.setattr(saved_views, "_validated", revoke)
    response = send(owner, "post", "/api/v1/saved-views", definition(owner))
    assert response.status_code == 401
    assert not SavedView.objects.exists()


def test_listing_pages_and_cursor_identity_are_bounded(authenticated_client):
    owner, _ = authenticated_client("analyst")
    first = create(owner, name="First")
    second = create(owner, name="Second")
    page = owner.get("/api/v1/saved-views", {"limit": 1}).json()
    assert [item["id"] for item in page["views"]] == [second["id"]]
    next_page = owner.get("/api/v1/saved-views", {"limit": 1, "cursor": page["next_cursor"]}).json()
    assert [item["id"] for item in next_page["views"]] == [first["id"]]
    assert next_page["next_cursor"] is None
    other, _ = authenticated_client("analyst2")
    assert (
        other.get("/api/v1/saved-views", {"limit": 1, "cursor": page["next_cursor"]}).status_code
        == 400
    )
    assert (
        owner.get("/api/v1/saved-views", {"limit": 2, "cursor": page["next_cursor"]}).status_code
        == 400
    )
    assert owner.get("/api/v1/saved-views", {"limit": 51}).status_code == 400


def test_owner_quota_is_checked_in_the_mutation_transaction(authenticated_client):
    owner, user = authenticated_client("analyst")
    model_id = owner.get("/api/v1/catalog").json()["model_id"]
    SavedView.objects.bulk_create(
        [
            SavedView(
                owner=user,
                name=f"View {index}",
                dataset_id="fleet_daily",
                sql="SELECT * FROM fleet_daily",
                created_model_id=model_id,
            )
            for index in range(50)
        ]
    )
    response = send(owner, "post", "/api/v1/saved-views", definition(owner))
    assert response.status_code == 409 and response.json()["code"] == "saved_view_limit"
    assert SavedView.objects.filter(owner=user).count() == 50


def test_definition_uses_explicit_adopted_model_without_pin_drift(
    authenticated_client,
    api_context,
    api_data_dir,
    tmp_path,
):
    target = tmp_path / "updated-models"
    shutil.copytree(api_data_dir, target)
    api_context.OUTAGE_DATA_DIR = target
    owner, _ = authenticated_client("analyst")
    view = create(owner)
    old = view["created_model_id"]
    ingest(IngestConfig(target, "recent"), FakeClient())
    current = owner.get("/api/v1/catalog").json()["model_id"]
    assert current != old
    assert run(owner, view, model_id=old).json()["result"]["model_id"] == old
    assert run(owner, view, model_id=current).json()["result"]["model_id"] == current
    assert SavedView.objects.get(pk=view["id"]).created_model_id == old


def test_transport_enforces_authentication_csrf_and_closed_run_fields(authenticated_client):
    owner, _ = authenticated_client("analyst")
    view = create(owner)
    response = send(
        owner,
        "post",
        location(view) + "/run",
        {
            "revision": 1,
            "model_id": view["created_model_id"],
            "sql": "SELECT * FROM generator_daily",
        },
    )
    assert response.status_code == 400
    owner.defaults.pop("HTTP_X_CSRFTOKEN")
    assert send(owner, "delete", location(view), {"revision": 1}).status_code == 403


def test_concurrent_definition_writer_returns_retryable_busy_without_saving(
    authenticated_client,
    api_context,
):
    owner, _ = authenticated_client("analyst")
    with writer_lock(api_context.OUTAGE_STATE_DIR / "saved-view-writes"):
        response = send(owner, "post", "/api/v1/saved-views", definition(owner))
    assert response.status_code == 503 and response.json()["code"] == "saved_views_busy"
    assert response["Retry-After"] == "1" and not SavedView.objects.exists()


def test_sqlite_contention_is_retryable_and_rolls_back_definition(
    authenticated_client, monkeypatch
):
    owner, _ = authenticated_client("analyst")

    def busy(*args, **kwargs):
        raise OperationalError("database is locked")

    monkeypatch.setattr(SavedView.objects, "create", busy)
    response = send(owner, "post", "/api/v1/saved-views", definition(owner))
    assert response.status_code == 503 and response.json()["code"] == "saved_views_busy"
    assert not SavedView.objects.exists()
