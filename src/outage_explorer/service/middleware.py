"""Correlate safe operational events without logging request bodies or SQL."""

from __future__ import annotations

import json
import logging
import time
import uuid

from django.utils.deprecation import MiddlewareMixin

from .errors import APIError, problem

LOG = logging.getLogger("outage_explorer.http")


class RequestMiddleware(MiddlewareMixin):
    def process_request(self, request):
        request.request_id = uuid.uuid4().hex
        request._outage_started = time.monotonic()

    def process_response(self, request, response):
        response["X-Request-ID"] = getattr(request, "request_id", "")
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
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
        if isinstance(exception, APIError):
            return problem(request, exception)
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
