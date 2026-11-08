"""Serve identity-only HTML shells and allowlisted packaged browser assets.

These views and the authenticated service API are applications in one Django
project. HTML rendering never loads analytical data. Persisted actor policy is
rechecked after private rendering, while each later JSON request is separately
authorized by the service. Only the explicit asset set is publicly cacheable.
"""

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

# Logical filenames are capabilities: do not replace this map with a directory lookup.
ASSETS = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "login.js": "text/javascript; charset=utf-8",
    "api.js": "text/javascript; charset=utf-8",
    "dom.js": "text/javascript; charset=utf-8",
    "table.js": "text/javascript; charset=utf-8",
    "chart.js": "text/javascript; charset=utf-8",
    "sql.js": "text/javascript; charset=utf-8",
    "sql_examples.js": "text/javascript; charset=utf-8",
    "sql_editor.bundle.js": "text/javascript; charset=utf-8",
    "sql_editor.LICENSE.txt": "text/plain; charset=utf-8",
    "query_chart.js": "text/javascript; charset=utf-8",
    "query_results.js": "text/javascript; charset=utf-8",
    "saved_views.js": "text/javascript; charset=utf-8",
    "refresh.js": "text/javascript; charset=utf-8",
    "users.js": "text/javascript; charset=utf-8",
    "overview.js": "text/javascript; charset=utf-8",
    "explore.js": "text/javascript; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}


def _actor(request):
    """Resolve the current persisted product actor, or return None on policy denial.

    HTML routes redirect instead of exposing the APIError payload. This helper
    delegates session, active-user and persona validation to service policy.
    """
    try:
        return identity(request)
    except APIError:
        return None


def _login_redirect():
    """Redirect to the fixed named login route without accepting a return URL."""
    return HttpResponseRedirect(reverse("frontend:login"))


@require_GET
@ensure_csrf_cookie
def home(request):
    """Render the private workspace shell for a currently authorized product actor.

    Context contains identity, persona capabilities and an opaque session scope,
    never analytical rows or credentials. Recheck persisted identity after
    rendering so a concurrent logout or role change cannot deliver this shell.
    Missing users and policy failures redirect to the fixed login route.
    """
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
        "sql_style_nonce": getattr(request, "outage_style_nonce", ""),
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
    """Render the public GET-only login shell, or redirect an authorized actor home.

    The browser performs CSRF-protected JSON authentication against the sibling
    API. The HTML form action does not implement a non-JavaScript login path.
    """
    if _actor(request) is not None:
        return HttpResponseRedirect(reverse("frontend:home"))
    return render(request, "outage_explorer/login.html")


@require_http_methods(["GET", "HEAD"])
def asset(request, filename):
    """Return one allowlisted packaged asset with MIME type and SHA-256 ETag.

    Unknown names and unavailable resources raise Http404 before arbitrary path
    lookup. GET returns bytes, HEAD retains their length without a body, and a
    matching If-None-Match produces 304. The public-asset marker lets shared
    middleware distinguish these cacheable resources from private HTML/JSON.
    """
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
