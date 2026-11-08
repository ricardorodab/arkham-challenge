"""Store private/shared analytical definitions and execute them with reader permissions.

Definitions contain SQL and presentation metadata, never analytical result rows. Sharing
adds visibility to a definition; every access still requires the reader's dataset policy.
The existing compiler/WASM service owns execution, row caps, model pins and query cursors.
"""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from datetime import datetime

from django.conf import settings
from django.core import signing
from django.db import OperationalError, transaction
from django.db.models import Q

from outage_explorer.data.storage import StorageError, writer_lock

from . import api_services
from .admission import query_slot
from .errors import APIError
from .models import SavedView
from .policy import Actor, authorize_dataset, permitted, recheck

ROLES = ("viewer", "analyst", "admin")
MAX_OWNED = 50
CURSOR_SALT = "outage-saved-views-v1"
EDIT_FIELDS = {"name", "dataset_id", "sql", "max_rows", "chart", "shared_roles", "model_id"}
NUMERIC_TYPE = re.compile(
    r"^(?:U?(?:TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|INT|FLOAT|REAL|DOUBLE|DECIMAL\(\d+,\d+\))$"
)


def can_create(actor: Actor) -> bool:
    """Limit saved-query authorship to Analysts and Admins without changing direct SQL policy."""
    return actor.role in {"analyst", "admin"}


def _require_author(actor: Actor) -> None:
    """Reject saved-query creation/editing by a role without authorship capability."""
    if not can_create(actor):
        raise APIError(
            403, "saved_view_author_required", "Creating saved views requires an Analyst or Admin."
        )


def _unavailable() -> APIError:
    """Use one absence/authorization response to avoid exposing another user's definitions."""
    return APIError(404, "saved_view_not_found", "The saved view is unavailable.")


def _revision(value) -> int:
    """Require the version supplied by the client rather than silently overwriting changes."""
    return api_services.integer(value, "revision", 1, 2_147_483_647)


def _model(value) -> str:
    """Require one explicit immutable model selected by the reader's workspace."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise APIError(400, "invalid_parameter", "model_id must identify the adopted publication.")
    return value


def _visible(actor: Actor):
    """Filter in SQL by current dataset permission and owner/audience visibility."""
    audience = Q(**{f"share_{actor.role}": True}) if actor.role in ROLES else Q(pk__in=[])
    return SavedView.objects.filter(
        Q(owner_id=actor.user_id) | audience, dataset_id__in=permitted(actor)
    )


def _get(actor: Actor, view_id, *, owned=False) -> SavedView:
    """Resolve an allowed definition; owner mutations additionally require ownership."""
    query = _visible(actor)
    if owned:
        query = query.filter(owner_id=actor.user_id)
    view = query.filter(pk=view_id).first()
    if view is None:
        raise _unavailable()
    return view


def public(view: SavedView, actor: Actor) -> dict:
    """Expose authorized definition metadata without disclosing another account's identity."""
    own = view.owner_id == actor.user_id
    return {
        "id": str(view.id),
        "name": view.name,
        "dataset_id": view.dataset_id,
        "sql": view.sql,
        "max_rows": view.max_rows,
        "chart": view.chart,
        "shared_roles": [role for role in ROLES if getattr(view, f"share_{role}")],
        "revision": view.revision,
        "is_owner": own,
        "can_edit": own and can_create(actor),
        "can_delete": own,
        "created_model_id": view.created_model_id,
        "created_at": view.created_at.isoformat(),
        "updated_at": view.updated_at.isoformat(),
    }


def _chart(value) -> dict:
    """Normalize one bounded presentation specification; output types are checked separately."""
    keys = {"enabled", "date_column", "value_column", "series_column"}
    if not isinstance(value, dict) or set(value) - keys or type(value.get("enabled")) is not bool:
        raise APIError(400, "invalid_chart", "Choose a supported chart configuration.")
    if not value["enabled"]:
        return {"enabled": False}
    date_column = api_services.text(value.get("date_column"), "date_column", 128)
    value_column = api_services.text(value.get("value_column"), "value_column", 128)
    series_column = value.get("series_column")
    if series_column is not None:
        series_column = api_services.text(series_column, "series_column", 128)
    fields = [date_column, value_column] + ([series_column] if series_column else [])
    if len(set(fields)) != len(fields):
        raise APIError(400, "invalid_chart", "Chart fields must identify different output columns.")
    return {
        "enabled": True,
        "date_column": date_column,
        "value_column": value_column,
        "series_column": series_column,
    }


def validate_chart(chart: dict, result: dict) -> None:
    """Require one date axis and numeric measure from the actual executed output schema."""
    if not chart["enabled"]:
        return
    columns = result.get("columns", [])
    names = [item["name"] for item in columns]
    if len(names) != len(set(names)):
        raise APIError(400, "invalid_chart", "Chart output columns must have unique names.")
    types = {item["name"]: item["type"].upper().replace(" ", "") for item in columns}
    if types.get(chart["date_column"]) != "DATE" or not NUMERIC_TYPE.fullmatch(
        types.get(chart["value_column"], "")
    ):
        raise APIError(
            400, "invalid_chart", "Choose a DATE output column and a numeric output column."
        )
    series_column = chart["series_column"]
    if series_column is not None:
        if series_column not in types:
            raise APIError(400, "invalid_chart", "The series field must be an output column.")
        if types[series_column] == "DATE" or types[series_column].startswith("TIMESTAMP"):
            raise APIError(
                400, "invalid_chart", "Choose a non-temporal output column for the series field."
            )


def _normalize(actor: Actor, payload: dict) -> dict:
    """Validate editable metadata before any compiler or analytical-file access."""
    name = api_services.text(payload.get("name"), "name", 100).strip()
    if not name or any(ord(character) < 32 for character in name):
        raise APIError(400, "invalid_parameter", "name must be a nonempty single-line label.")
    dataset_id = api_services.text(payload.get("dataset_id"), "dataset_id", 64)
    authorize_dataset(actor, dataset_id)
    sql = api_services.text(payload.get("sql"), "sql", 16_384)
    if not sql.strip() or len(sql.encode("utf-8")) > 16_384:
        raise APIError(400, "invalid_parameter", "SQL must fit within the supported query limit.")
    roles = payload.get("shared_roles", [])
    if (
        not isinstance(roles, list)
        or len(roles) > len(ROLES)
        or any(not isinstance(role, str) or role not in ROLES for role in roles)
        or len(set(roles)) != len(roles)
    ):
        raise APIError(400, "invalid_audience", "Choose distinct supported audience roles.")
    for role in roles:
        if dataset_id not in permitted(Actor(actor.user_id, role, actor.binding)):
            raise APIError(
                400, "invalid_audience", "This dataset cannot be shared with that audience."
            )
    return {
        "name": name,
        "dataset_id": dataset_id,
        "sql": sql,
        "max_rows": api_services.integer(payload.get("max_rows", 1000), "max_rows", 1, 1000),
        "chart": _chart(payload.get("chart", {"enabled": False})),
        **{f"share_{role}": role in roles for role in ROLES},
    }


def _validated(request, actor: Actor, definition: dict, model_id: str) -> None:
    """Validate SQL/model and chart output outside the short definition-write transaction."""
    with query_slot(actor.user_id):
        recheck(request, actor)
        if definition["chart"]["enabled"]:
            result = api_services.query(
                request,
                actor,
                {key: definition[key] for key in ("dataset_id", "sql", "max_rows")}
                | {"model_id": model_id, "page_size": 1},
            )
            validate_chart(definition["chart"], result)
        else:
            api_services._validate(
                actor, definition["sql"], definition["dataset_id"], definition["max_rows"]
            )
            api_services._bundle(model_id)
        recheck(request, actor)


@contextmanager
def _writing():
    """Serialize bounded local definition mutations and quota checks across processes."""
    try:
        with writer_lock(settings.OUTAGE_STATE_DIR / "saved-view-writes"), transaction.atomic():
            yield
    except StorageError as error:
        if error.code != "already_running":
            raise
        raise APIError(
            503, "saved_views_busy", "Saved views are busy; retry shortly.", retry_after=1
        ) from None
    except OperationalError as error:
        if not any(word in str(error).lower() for word in ("locked", "busy")):
            raise
        raise APIError(
            503, "saved_views_busy", "Saved views are busy; retry shortly.", retry_after=1
        ) from None


def listing(request, actor: Actor, parameters: dict) -> dict:
    """Page authorized definitions using an actor-bound signed immutable creation-order cursor."""
    limit = api_services.integer(parameters.get("limit", 20), "limit", 1, 50)
    query = _visible(actor)
    token = parameters.get("cursor")
    if token is not None:
        try:
            if not isinstance(token, str) or len(token) > 4096:
                raise ValueError
            decoded = signing.loads(token, salt=CURSOR_SALT, max_age=900)
            if (
                not isinstance(decoded, dict)
                or set(decoded) != {"actor", "limit", "created", "id"}
                or decoded["actor"] != actor.binding
                or decoded["limit"] != limit
                or not isinstance(decoded["created"], str)
                or not isinstance(decoded["id"], str)
            ):
                raise ValueError
            stamp = datetime.fromisoformat(decoded["created"])
            item_id = uuid.UUID(decoded["id"])
            if stamp.tzinfo is None:
                raise ValueError
        except (signing.BadSignature, ValueError, TypeError, KeyError, OverflowError):
            raise APIError(
                400, "invalid_cursor", "The saved-view cursor is invalid or expired."
            ) from None
        query = query.filter(Q(created_at__lt=stamp) | Q(created_at=stamp, id__lt=item_id))
    items = list(query.order_by("-created_at", "-id")[: limit + 1])
    following = None
    if len(items) > limit:
        items = items[:limit]
        last = items[-1]
        following = signing.dumps(
            {
                "actor": actor.binding,
                "limit": limit,
                "created": last.created_at.isoformat(),
                "id": str(last.id),
            },
            salt=CURSOR_SALT,
            compress=True,
        )
    recheck(request, actor)
    return {
        "views": [public(item, actor) for item in items],
        "next_cursor": following,
        "can_create": can_create(actor),
        "can_share": can_create(actor),
        "max_owned": MAX_OWNED,
    }


def retrieve(request, actor: Actor, view_id) -> dict:
    """Return one currently visible definition without enumerating hidden objects."""
    view = _get(actor, view_id)
    recheck(request, actor)
    return public(view, actor)


def create(request, actor: Actor, payload: dict) -> dict:
    """Validate a definition, then atomically add one owner-scoped saved view."""
    _require_author(actor)
    definition = _normalize(actor, payload)
    model_id = _model(payload.get("model_id"))
    _validated(request, actor, definition, model_id)
    with _writing():
        recheck(request, actor)
        _require_author(actor)
        if SavedView.objects.filter(owner_id=actor.user_id).count() >= MAX_OWNED:
            raise APIError(
                409,
                "saved_view_limit",
                "Delete a saved view before adding another; the limit is 50.",
            )
        view = SavedView.objects.create(
            owner_id=actor.user_id, created_model_id=model_id, **definition
        )
        recheck(request, actor)
        return public(view, actor)


def update(request, actor: Actor, view_id, payload: dict) -> dict:
    """Replace an owned definition with optimistic concurrency and fresh SQL/chart validation."""
    _require_author(actor)
    original = _get(actor, view_id, owned=True)
    revision = _revision(payload.get("revision"))
    if original.revision != revision:
        raise APIError(409, "saved_view_changed", "The saved view changed; reload before editing.")
    definition = _normalize(actor, payload)
    _validated(request, actor, definition, _model(payload.get("model_id")))
    with _writing():
        recheck(request, actor)
        view = _get(actor, view_id, owned=True)
        if view.revision != revision:
            raise APIError(
                409, "saved_view_changed", "The saved view changed; reload before editing."
            )
        for key, value in definition.items():
            setattr(view, key, value)
        view.revision += 1
        view.save()
        recheck(request, actor)
        return public(view, actor)


def delete(request, actor: Actor, view_id, payload: dict) -> dict:
    """Delete an owned definition, retaining all analytical publications and other owners' views."""
    revision = _revision(payload.get("revision"))
    with _writing():
        recheck(request, actor)
        view = _get(actor, view_id, owned=True)
        if view.revision != revision:
            raise APIError(
                409, "saved_view_changed", "The saved view changed; reload before deleting."
            )
        view.delete()
        recheck(request, actor)
    return {"deleted": True}


def run(request, actor: Actor, view_id, payload: dict) -> dict:
    """Execute with reader identity and reject edits/unsharing that occur during execution."""
    view = _get(actor, view_id)
    revision = _revision(payload.get("revision"))
    if view.revision != revision:
        raise APIError(409, "saved_view_changed", "The saved view changed; reload before running.")
    query = {
        "dataset_id": view.dataset_id,
        "sql": view.sql,
        "max_rows": view.max_rows,
        "model_id": _model(payload.get("model_id")),
        "page_size": api_services.integer(
            payload.get("page_size", min(100, view.max_rows)), "page_size", 1, view.max_rows
        ),
    }
    if "cursor" in payload:
        query["cursor"] = payload["cursor"]
    result = api_services.query(request, actor, query)
    validate_chart(view.chart, result)
    current = _get(actor, view_id)
    if current.revision != revision:
        raise APIError(409, "saved_view_changed", "The saved view changed; reload before running.")
    recheck(request, actor)
    return {"view": public(current, actor), "result": result}
