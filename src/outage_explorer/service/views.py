"""Versioned JSON capabilities; browser sessions use CSRF on every mutation."""

from __future__ import annotations

import json
from functools import wraps
from importlib.resources import files

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import ensure_csrf_cookie

from . import api_services
from .authentication import limit_login
from .errors import APIError, problem
from .models import RefreshJob
from .policy import identity, recheck, require_admin
from .refresh import RefreshServiceError, enqueue_refresh


def response(payload, *, status=200):
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode()) > settings.OUTAGE_QUERY_MAX_BYTES:
        raise APIError(413, "result_too_large", "The result exceeds the response size limit.")
    return JsonResponse(
        payload, status=status, json_dumps_params={"allow_nan": False, "separators": (",", ":")}
    )


def endpoint(*methods, authenticated=True):
    def decorate(function):
        @wraps(function)
        def call(request, *args, **kwargs):
            try:
                actor = identity(request) if authenticated else None
                if request.method not in methods:
                    result = problem(
                        request,
                        APIError(405, "method_not_allowed", "This HTTP method is not supported."),
                    )
                    result["Allow"] = ", ".join(methods)
                    return result
                return function(request, actor, *args, **kwargs)
            except APIError as error:
                return problem(request, error)

        return call

    return decorate


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def body(request, allowed):
    if request.content_type != "application/json":
        raise APIError(415, "json_required", "Send an application/json request body.")
    try:
        raw = request.body
        if len(raw) > 32768:
            raise APIError(413, "request_too_large", "The request body is too large.")
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(payload, dict) or set(payload) - set(allowed):
            raise ValueError
        return payload
    except RequestDataTooBig:
        raise APIError(413, "request_too_large", "The request body is too large.") from None
    except (UnicodeError, ValueError, RecursionError):
        raise APIError(
            400, "invalid_json", "Send one JSON object with supported fields and no duplicate keys."
        ) from None


def parameters(request, allowed):
    if set(request.GET) - set(allowed) or any(
        len(values) != 1 for _, values in request.GET.lists()
    ):
        raise APIError(
            400, "invalid_parameter", "Query parameters contain unsupported or duplicate fields."
        )
    return request.GET.dict()


@ensure_csrf_cookie
@endpoint("GET", authenticated=False)
def csrf(request, actor):
    return response({"csrf_token": get_token(request)})


@endpoint("POST", authenticated=False)
def login(request, actor):
    payload = body(request, {"username", "password"})
    username = api_services.text(payload.get("username"), "username", 150)
    password = api_services.text(payload.get("password"), "password", 1024)
    limit_login(request, username)
    user = authenticate(request, username=username, password=password)
    role = getattr(getattr(user, "persona", None), "role", None) if user else None
    if user is None or role not in {"viewer", "analyst", "admin"}:
        raise APIError(401, "invalid_credentials", "The credentials are invalid.")
    django_login(request, user)
    return response(
        {"user": {"username": user.username, "persona": role}, "csrf_token": get_token(request)}
    )


@endpoint("POST")
def logout(request, actor):
    body(request, set())
    django_logout(request)
    return response({"signed_out": True})


@endpoint("GET")
def session(request, actor):
    parameters(request, set())
    return response({"user": {"username": request.user.username, "persona": actor.role}})


@endpoint("GET")
def catalog(request, actor):
    args = parameters(request, {"model_id"})
    return response(api_services.catalog(request, actor, args.get("model_id")))


@endpoint("GET")
def data(request, actor, dataset_id):
    args = parameters(request, {"start", "end", "facility_id", "limit", "cursor", "model_id"})
    return response(api_services.preview(request, actor, dataset_id, args))


@endpoint("POST")
def query(request, actor):
    payload = body(request, {"dataset_id", "sql", "max_rows", "page_size", "cursor", "model_id"})
    return response(api_services.query(request, actor, payload))


def _job(job):
    return {
        "id": str(job.id),
        "mode": job.mode,
        "status": job.status,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error_code": job.error_code or None,
        "message": job.public_message,
        "result": job.result,
        "status_url": f"/api/v1/refresh/{job.id}",
    }


@endpoint("POST", "GET")
def refresh(request, actor):
    require_admin(actor)
    if request.method == "GET":
        args = parameters(request, {"limit"})
        limit = api_services.integer(args.get("limit", 10), "limit", 1, 50)
        jobs = RefreshJob.objects.filter(owner_id=actor.user_id).order_by("-created_at")[:limit]
        recheck(request, actor)
        return response({"jobs": [_job(job) for job in jobs]})
    payload = body(request, {"mode"})
    try:
        job, created = enqueue_refresh(
            request.user, payload.get("mode", "recent"), request.headers.get("Idempotency-Key")
        )
    except RefreshServiceError as error:
        status = 409 if error.code in {"refresh_busy", "idempotency_conflict"} else 400
        if error.code == "forbidden":
            status = 403
        raise APIError(
            status,
            error.code,
            error.public_message,
            retry_after=5 if error.code == "refresh_busy" else None,
        ) from None
    recheck(request, actor)
    result = response(_job(job), status=202 if created else 200)
    result["Location"] = f"/api/v1/refresh/{job.id}"
    return result


@endpoint("GET")
def refresh_status(request, actor, job_id):
    require_admin(actor)
    parameters(request, set())
    job = RefreshJob.objects.filter(pk=job_id, owner_id=actor.user_id).first()
    if job is None:
        raise APIError(404, "job_not_found", "The refresh job is unavailable.")
    recheck(request, actor)
    return response(_job(job))


@endpoint("GET", authenticated=False)
def live(request, actor):
    return response({"status": "ok"})


@endpoint("GET")
def ready(request, actor):
    require_admin(actor)
    bundle = api_services._bundle(None)
    from outage_explorer.query_sandbox import runtime_status

    runtime = runtime_status()
    if not runtime["available"]:
        raise APIError(503, "sandbox_unavailable", "The isolated query runtime is unavailable.")
    recheck(request, actor)
    return response({"status": "ready", "model_id": bundle.id, "query_runtime": runtime})


@endpoint("GET")
def openapi(request, actor):
    return response(
        json.loads(
            files("outage_explorer.service").joinpath("openapi.json").read_text(encoding="utf-8")
        )
    )


def csrf_failure(request, reason=""):
    return problem(
        request,
        APIError(403, "csrf_failed", "A valid CSRF token and same-origin request are required."),
    )


def not_found(request, exception=None):
    return problem(request, APIError(404, "not_found", "This endpoint does not exist."))


def bad_request(request, exception=None):
    return problem(request, APIError(400, "bad_request", "The request is invalid."))


def server_error(request):
    return problem(request, APIError(500, "internal_error", "The request could not be completed."))
