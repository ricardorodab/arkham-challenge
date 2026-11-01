"""Private HTML shells and a fixed public asset set; no analytical data access."""

from __future__ import annotations

import hashlib
from importlib.resources import files

from django.contrib.auth import get_user_model
from django.http import Http404, HttpResponse, HttpResponseNotModified, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import parse_etags
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from outage_explorer.service.errors import APIError
from outage_explorer.service.policy import frontend_session_scope, identity, recheck

ASSETS = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "login.js": "text/javascript; charset=utf-8",
    "api.js": "text/javascript; charset=utf-8",
    "dom.js": "text/javascript; charset=utf-8",
    "table.js": "text/javascript; charset=utf-8",
    "chart.js": "text/javascript; charset=utf-8",
    "sql.js": "text/javascript; charset=utf-8",
    "refresh.js": "text/javascript; charset=utf-8",
    "overview.js": "text/javascript; charset=utf-8",
    "explore.js": "text/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}


def _actor(request):
    try:
        return identity(request)
    except APIError:
        return None


def _login_redirect():
    # No user-supplied return URL is accepted or reflected.
    return HttpResponseRedirect(reverse("frontend:login"))


@require_GET
@ensure_csrf_cookie
def home(request):
    actor = _actor(request)
    if actor is None:
        return _login_redirect()
    username = (
        get_user_model().objects.filter(pk=actor.user_id).values_list("username", flat=True).first()
    )
    if username is None:
        return _login_redirect()
    scope = frontend_session_scope(actor)
    context = {
        "username": username,
        "persona": actor.role,
        "is_viewer": actor.role == "viewer",
        "is_analyst": actor.role in {"analyst", "admin"},
        "is_admin": actor.role == "admin",
        "session_scope": scope,
        "frontend_session": {
            "username": username,
            "persona": actor.role,
            "session_scope": scope,
        },
    }
    response = render(request, "outage_explorer/app.html", context)
    try:
        recheck(request, actor)
    except APIError:
        return _login_redirect()
    return response


@require_GET
@ensure_csrf_cookie
def login(request):
    if _actor(request) is not None:
        return HttpResponseRedirect(reverse("frontend:home"))
    return render(request, "outage_explorer/login.html")


@require_http_methods(["GET", "HEAD"])
def asset(request, filename):
    content_type = ASSETS.get(filename)
    if content_type is None:
        raise Http404
    try:
        payload = (
            files("outage_explorer.frontend")
            .joinpath("static", "outage_explorer", filename)
            .read_bytes()
        )
    except (OSError, ValueError):
        raise Http404 from None
    etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
    accepted = parse_etags(request.headers.get("If-None-Match", ""))
    if etag in accepted or "*" in accepted:
        response = HttpResponseNotModified()
    else:
        response = HttpResponse(
            payload if request.method == "GET" else b"", content_type=content_type
        )
        response["Content-Length"] = str(len(payload))
    response["ETag"] = etag
    response["Cache-Control"] = "public, max-age=0, must-revalidate"
    response._outage_public_asset = True
    return response
