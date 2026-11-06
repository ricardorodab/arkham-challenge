"""Attach request correlation, safe operational logging and browser response policy.

MiddlewareMixin invokes the process hooks through Django's supported middleware lifecycle.
Request attributes are transient in-memory state. Logs record route, method, status and
duration, never request bodies, SQL or raw exception text.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from django.http import Http404
from django.utils.deprecation import MiddlewareMixin

from .errors import APIError, problem

LOG = logging.getLogger("outage_explorer.http")
HTML_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


class RequestMiddleware(MiddlewareMixin):
    """Apply consistent correlation, cache policy and security/error formatting."""

    def process_request(self, request):
        """Assign an opaque request ID and monotonic start time for this request only."""
        request.request_id = uuid.uuid4().hex
        request._outage_started = time.monotonic()

    def process_response(self, request, response):
        """Attach correlation/security headers and emit one bounded request event.

        Default to no-store; only explicitly marked public static assets retain their own
        revalidation caching. HTML gets the self-only application CSP, while JSON receives a
        no-content policy. Log the resolved route pattern rather than URLs or request
        parameters that could contain sensitive values.
        """
        response["X-Request-ID"] = getattr(request, "request_id", "")
        if not getattr(response, "_outage_public_asset", False):
            response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = (
            HTML_CSP
            if response.get("Content-Type", "").startswith("text/html")
            else "default-src 'none'; frame-ancestors 'none'"
        )
        match = getattr(request, "resolver_match", None)
        LOG.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": getattr(request, "request_id", ""),
                    "method": request.method,
                    "route": getattr(match, "route", "unmatched"),
                    "status": response.status_code,
                    "duration_ms": round(
                        1000
                        * (
                            time.monotonic() - getattr(request, "_outage_started", time.monotonic())
                        ),
                        2,
                    ),
                }
            )
        )
        return response

    def process_exception(self, request, exception):
        """Map APIError or unexpected failures to safe JSON; let Django handle Http404.

        Unexpected failures log only exception type and request ID, then return a generic 500.
        Returning None for Http404 preserves Django's configured not-found handler.
        """
        if isinstance(exception, APIError):
            return problem(request, exception)
        if isinstance(exception, Http404):
            return None
        LOG.error(
            json.dumps(
                {
                    "event": "http_failure",
                    "request_id": getattr(request, "request_id", ""),
                    "error_type": type(exception).__name__,
                }
            )
        )
        return problem(
            request, APIError(500, "internal_error", "The request could not be completed.")
        )
