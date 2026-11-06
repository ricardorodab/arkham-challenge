"""Derive current server identities and apply the shared dataset permission policy.

Both JSON endpoints and protected HTML views use this module. Product roles come from the
persisted Persona, not request fields or Django staff privileges. Actor bindings allow long
operations, cache contexts and cursors to detect a changed account, role, password or session
before returning private results.
"""

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
    """Immutable authorization snapshot for one user, product role and session.

    binding is an HMAC of account ID, role, session key and authentication hash. It identifies
    an authorization context without exposing the underlying session credential. Equality with
    a freshly derived Actor is required at recheck points.
    """

    user_id: int
    role: str
    binding: str


def frontend_session_scope(actor: Actor) -> str:
    """Derive an opaque browser-state marker from an Actor binding.

    The returned HMAC scopes retained browser history to the current server session. It is not
    a session credential and cannot authenticate a request.
    """
    return salted_hmac("outage-frontend-session", actor.binding, algorithm="sha256").hexdigest()


def identity(request) -> Actor:
    """Return a fresh Actor or raise APIError for invalid authentication or persona.

    Reload the active user and Persona, then require an unexpired persisted Django Session
    whose user/backend/hash agree with both the request and the user's current password hash.
    Checking the database row detects session deletion during an in-flight request;
    request.user and already-loaded session fields alone do not.
    """
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
    """Reject revocation or any identity-context change since original was captured.

    identity may raise a 401/403 itself. A different valid Actor raises permissions_changed;
    callers use this before delivering or caching results.
    """
    current = identity(request)
    if current != original:
        raise APIError(403, "permissions_changed", "Permissions changed; repeat the request.")


def permitted(actor: Actor) -> frozenset[str]:
    """Return the immutable set of logical dataset names allowed to actor.

    Analyst/Admin receive all registered datasets; Viewer receives only datasets classified
    national. Unknown roles receive none, and new datasets default to detail classification in
    the model contract.
    """
    return frozenset(
        key
        for key, spec in DATASETS.items()
        if actor.role in {"analyst", "admin"}
        or (actor.role == "viewer" and spec.classification == "national")
    )


def authorize_dataset(actor: Actor, dataset_id: str) -> None:
    """Raise the same 403 for unknown and unauthorized dataset names.

    Call before analytical file access; the caller receives no existence oracle for datasets
    outside its permitted set.
    """
    if dataset_id not in permitted(actor):
        raise APIError(403, "dataset_forbidden", "Dataset access is not permitted.")


def require_admin(actor: Actor) -> None:
    """Require the server-derived Admin persona or raise a 403 APIError."""
    if actor.role != "admin":
        raise APIError(403, "admin_required", "This capability requires an Admin persona.")
