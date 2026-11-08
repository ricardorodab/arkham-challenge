"""Manage product accounts through fresh Admin policy and bounded database writes.

Passwords are generated on the server and returned once; only Django password hashes
are persisted. Opaque edit versions detect concurrent changes without exposing hashes.
Session epochs invalidate all previous sessions in constant work per affected user.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from contextlib import contextmanager

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F, Q
from django.utils.crypto import constant_time_compare, salted_hmac

from outage_explorer.data.storage import StorageError, writer_lock

from . import api_services
from .errors import APIError
from .models import Persona, RefreshJob
from .policy import Actor, recheck, require_admin

LOG = logging.getLogger(__name__)
ROLES = frozenset(Persona.Role.values)
EDIT_FIELDS = frozenset({"username", "first_name", "last_name", "role", "is_active"})
CURSOR_SALT = "outage-admin-users-v1"


def _accounts():
    """Restrict management to product accounts without infrastructure privileges."""
    return (
        get_user_model()
        .objects.select_related("persona")
        .filter(is_staff=False, is_superuser=False, persona__role__in=ROLES)
    )


def _version(user) -> str:
    """Fingerprint editable state and credentials without disclosing their inputs."""
    value = [
        user.pk,
        user.username,
        user.first_name,
        user.last_name,
        user.persona.role,
        user.is_active,
        user.password,
        user.persona.session_version,
        user.is_staff,
        user.is_superuser,
    ]
    return salted_hmac(
        "outage-admin-user-version", json.dumps(value, ensure_ascii=True), algorithm="sha256"
    ).hexdigest()


def public(user, actor: Actor) -> dict:
    """Return bounded account metadata, never password hashes or session credentials."""
    return {
        "id": user.pk,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "role": user.persona.role,
        "is_active": user.is_active,
        "is_self": user.pk == actor.user_id,
        "version": _version(user),
        "date_joined": user.date_joined.isoformat(),
        "last_login": user.last_login.isoformat() if user.last_login else None,
    }


def _get(user_id):
    """Resolve one manageable product account or report the same unavailable result."""
    user_id = api_services.integer(user_id, "user_id", 1, 2**63 - 1)
    user = _accounts().filter(pk=user_id).first()
    if user is None:
        raise APIError(404, "user_not_found", "The user account is unavailable.")
    return user


def _expected(user, supplied) -> None:
    """Reject a missing or stale edit version before changing the account."""
    if not isinstance(supplied, str) or re.fullmatch(r"[0-9a-f]{64}", supplied) is None:
        raise APIError(400, "invalid_version", "Supply the current account version.")
    if not constant_time_compare(supplied, _version(user)):
        raise APIError(409, "user_changed", "This account changed. Reload it before editing.")


def _normalize(payload: dict, *, creating=False) -> dict:
    """Validate the narrow account-edit vocabulary without accepting privilege flags."""
    result = {}
    if creating and not {"username", "role"} <= payload.keys():
        raise APIError(400, "invalid_parameter", "Username and role are required.")
    if not creating and not EDIT_FIELDS.intersection(payload):
        raise APIError(400, "invalid_parameter", "Supply at least one account field to update.")
    for name in ("username", "first_name", "last_name"):
        if name not in payload:
            continue
        value = payload[name]
        if (
            not isinstance(value, str)
            or len(value) > 150
            or not all(character.isprintable() for character in value)
        ):
            raise APIError(400, "invalid_parameter", f"{name} must contain at most 150 characters.")
        if name == "username":
            value = get_user_model().normalize_username(value)
            try:
                get_user_model()._meta.get_field("username").clean(value, None)
            except ValidationError:
                raise APIError(
                    400,
                    "invalid_parameter",
                    "Use a valid username of at most 150 letters, digits or @/./+/-/_ characters.",
                ) from None
        else:
            value = value.strip()
        result[name] = value
    if "role" in payload:
        if not isinstance(payload["role"], str) or payload["role"] not in ROLES:
            raise APIError(400, "invalid_parameter", "Choose Viewer, Analyst or Admin.")
        result["role"] = payload["role"]
    if "is_active" in payload:
        if type(payload["is_active"]) is not bool:
            raise APIError(400, "invalid_parameter", "is_active must be a boolean.")
        result["is_active"] = payload["is_active"]
    return result


def _password() -> tuple[str, str]:
    """Generate 192 bits of random password entropy and hash outside the write lock."""
    clear = secrets.token_urlsafe(24)
    validate_password(clear)
    return clear, make_password(clear)


@contextmanager
def _writing(request, actor: Actor):
    """Serialize account mutations and authorize inside the SQLite write boundary.

    The first database statement is a no-op write, so the transaction acquires its
    database write reservation before reading permissions or last-Admin counts.
    The OS lock bounds same-feature contention across local web processes.
    """
    try:
        with writer_lock(settings.OUTAGE_STATE_DIR / "user-management"), transaction.atomic():
            get_user_model().objects.filter(pk=actor.user_id).update(last_login=F("last_login"))
            recheck(request, actor)
            require_admin(actor)
            yield
    except StorageError as error:
        if error.code != "already_running":
            raise
        raise APIError(
            503, "users_busy", "Account management is busy; retry shortly.", retry_after=1
        ) from None
    except OperationalError as error:
        if not any(word in str(error).lower() for word in ("locked", "busy")):
            raise
        raise APIError(
            503, "users_busy", "Account management is busy; retry shortly.", retry_after=1
        ) from None


def _unique(username: str, excluding=None) -> None:
    """Reserve usernames across all Django accounts, including hidden infrastructure accounts."""
    query = get_user_model().objects.filter(username__iexact=username)
    if excluding is not None:
        query = query.exclude(pk=excluding)
    if query.exists():
        raise APIError(409, "username_unavailable", "That username is unavailable.")


def _protect(user, actor: Actor, role: str, active: bool, *, deleting=False) -> None:
    """Prevent self lockout and removing the last active product Administrator."""
    removes_admin = (
        user.persona.role == "admin"
        and user.is_active
        and (role != "admin" or not active or deleting)
    )
    if (
        removes_admin
        and not get_user_model()
        .objects.filter(is_active=True, is_staff=False, is_superuser=False, persona__role="admin")
        .exclude(pk=user.pk)
        .exists()
    ):
        raise APIError(409, "last_admin", "Keep at least one active Admin account.")
    if user.pk == actor.user_id and (deleting or role != "admin" or not active):
        raise APIError(409, "self_lockout", "Use another Admin account to change your own access.")


def _audit(actor: Actor, user_id: int, action: str) -> None:
    """Record committed account actions using numeric IDs without personal or secret values."""
    transaction.on_commit(
        lambda: LOG.info(
            json.dumps(
                {
                    "event": "account_management",
                    "action": action,
                    "actor_id": actor.user_id,
                    "user_id": user_id,
                }
            )
        )
    )


def listing(request, actor: Actor, parameters: dict) -> dict:
    """Search and page product accounts with an actor-bound signed ID seek cursor."""
    require_admin(actor)
    size = api_services.integer(parameters.get("page_size", 25), "page_size", 1, 100)
    search = parameters.get("search", "")
    if (
        not isinstance(search, str)
        or len(search) > 150
        or not all(character.isprintable() for character in search)
    ):
        raise APIError(400, "invalid_parameter", "Search must contain at most 150 characters.")
    query = _accounts()
    if search:
        query = query.filter(
            Q(username__icontains=search)
            | Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
        )
    token = parameters.get("cursor")
    if token is not None:
        try:
            if not isinstance(token, str) or len(token) > 4096:
                raise ValueError
            cursor = signing.loads(token, salt=CURSOR_SALT, max_age=900)
            if (
                cursor.get("actor") != actor.binding
                or cursor.get("search") != search
                or cursor.get("page_size") != size
                or type(cursor.get("after")) is not int
                or not 1 <= cursor["after"] <= 2**63 - 1
            ):
                raise ValueError
        except (signing.BadSignature, ValueError, TypeError, AttributeError, KeyError):
            raise APIError(
                400, "invalid_cursor", "The user-list cursor is invalid or expired."
            ) from None
        query = query.filter(pk__gt=cursor["after"])
    users = list(query.order_by("pk")[: size + 1])
    following = None
    if len(users) > size:
        users = users[:size]
        following = signing.dumps(
            {"actor": actor.binding, "search": search, "page_size": size, "after": users[-1].pk},
            salt=CURSOR_SALT,
            compress=True,
        )
    recheck(request, actor)
    return {"users": [public(user, actor) for user in users], "next_cursor": following}


def create(request, actor: Actor, payload: dict) -> dict:
    """Create a product-only account and reveal its generated password in this response only."""
    require_admin(actor)
    fields = _normalize(payload, creating=True)
    clear, hashed = _password()
    try:
        with _writing(request, actor):
            _unique(fields["username"])
            role = fields.pop("role")
            user = get_user_model().objects.create(password=hashed, **fields)
            Persona.objects.create(user=user, role=role)
            user = _get(user.pk)
            _audit(actor, user.pk, "created")
            result = {
                "user": public(user, actor),
                "generated_password": clear,
                "reauthenticate": False,
            }
    except IntegrityError:
        raise APIError(409, "username_unavailable", "That username is unavailable.") from None
    return result


def update(request, actor: Actor, user_id: int, payload: dict) -> dict:
    """Apply validated account fields only when the supplied edit version remains current."""
    require_admin(actor)
    fields = _normalize(payload)
    try:
        with _writing(request, actor):
            user = _get(user_id)
            _expected(user, payload.get("version"))
            role = fields.get("role", user.persona.role)
            active = fields.get("is_active", user.is_active)
            _protect(user, actor, role, active)
            if "username" in fields:
                _unique(fields["username"], excluding=user.pk)
            security_changed = role != user.persona.role or active != user.is_active
            for key, value in fields.items():
                if key != "role":
                    setattr(user, key, value)
            user.save(update_fields=[key for key in fields if key != "role"] or ["username"])
            user.persona.role = role
            if security_changed:
                user.persona.session_version += 1
            user.persona.save(update_fields=["role", "session_version"])
            _audit(actor, user.pk, "updated")
            result = {"user": public(user, actor), "reauthenticate": False}
    except IntegrityError:
        raise APIError(409, "username_unavailable", "That username is unavailable.") from None
    return result


def reset_password(request, actor: Actor, user_id: int, payload: dict) -> dict:
    """Replace a password, retire every old session, and return the new password once.

    A self-reset intentionally returns after retiring the initiating session. The
    mutation was authorized inside the same write transaction; a post-change
    identity recheck would discard the only copy of the new generated password.
    """
    require_admin(actor)
    clear, hashed = _password()
    with _writing(request, actor):
        user = _get(user_id)
        _expected(user, payload.get("version"))
        user.password = hashed
        user.save(update_fields=["password"])
        user.persona.session_version += 1
        user.persona.save(update_fields=["session_version"])
        _audit(actor, user.pk, "password_reset")
        result = {
            "user": public(user, actor),
            "generated_password": clear,
            "reauthenticate": user.pk == actor.user_id,
        }
    return result


def delete(request, actor: Actor, user_id: int, payload: dict) -> dict:
    """Delete an account while retaining refresh history and rejecting active owned work."""
    require_admin(actor)
    with _writing(request, actor):
        user = _get(user_id)
        _expected(user, payload.get("version"))
        _protect(user, actor, user.persona.role, user.is_active, deleting=True)
        if RefreshJob.objects.filter(owner_id=user.pk, status__in=["queued", "running"]).exists():
            raise APIError(
                409, "user_has_active_refresh", "Wait for this user's active refresh to finish."
            )
        _audit(actor, user.pk, "deleted")
        user.delete()
    return {"deleted": True}
