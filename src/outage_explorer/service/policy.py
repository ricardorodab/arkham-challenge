"""Server-derived identities and one default-deny analytical permission policy."""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from outage_explorer.model_contracts import DATASETS

from .errors import APIError


@dataclass(frozen=True)
class Actor:
    user_id: int
    role: str
    binding: str


def identity(request) -> Actor:
    if not request.user.is_authenticated:
        raise APIError(401, "authentication_required", "Sign in to use this capability.")
    user = get_user_model().objects.select_related("persona").filter(pk=request.user.pk).first()
    if user is None or not user.is_active:
        raise APIError(401, "session_revoked", "Sign in again.")
    session_hash = request.session.get("_auth_user_hash", "")
    persisted = Session.objects.filter(
        session_key=request.session.session_key, expire_date__gt=timezone.now()
    ).first()
    session = persisted.get_decoded() if persisted is not None else {}
    if (
        session.get("_auth_user_id") != str(user.pk)
        or not constant_time_compare(session.get("_auth_user_hash", ""), session_hash)
        or session.get("_auth_user_backend") != request.session.get("_auth_user_backend")
    ):
        raise APIError(401, "session_revoked", "Sign in again.")
    if not constant_time_compare(session_hash, user.get_session_auth_hash()):
        raise APIError(401, "session_revoked", "Sign in again.")
    role = getattr(getattr(user, "persona", None), "role", None)
    if role not in {"viewer", "analyst", "admin"}:
        raise APIError(403, "persona_required", "This account has no valid product persona.")
    binding = salted_hmac(
        "outage-actor", f"{user.pk}:{role}:{request.session.session_key}:{session_hash}"
    ).hexdigest()
    return Actor(user.pk, role, binding)


def recheck(request, original: Actor) -> None:
    current = identity(request)
    if current != original:
        raise APIError(403, "permissions_changed", "Permissions changed; repeat the request.")


def permitted(actor: Actor) -> frozenset[str]:
    return frozenset(
        key
        for key, spec in DATASETS.items()
        if actor.role in {"analyst", "admin"}
        or (actor.role == "viewer" and spec.classification == "national")
    )


def authorize_dataset(actor: Actor, dataset_id: str) -> None:
    if dataset_id not in permitted(actor):
        raise APIError(403, "dataset_forbidden", "Dataset access is not permitted.")


def require_admin(actor: Actor) -> None:
    if actor.role != "admin":
        raise APIError(403, "admin_required", "This capability requires an Admin persona.")
