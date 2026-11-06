"""Represent safe public API failures and serialize correlated Problem Details.

Callers choose stable codes/messages rather than forwarding untrusted SQL, filesystem paths,
credentials or raw engine exceptions to a browser.
"""

from __future__ import annotations

from django.http import JsonResponse


class APIError(Exception):
    """Carry a public HTTP status, stable code/detail and optional retry delay."""

    def __init__(self, status: int, code: str, detail: str, *, retry_after: int | None = None):
        """Store already-safe error fields; retry_after is keyword-only seconds."""
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail
        self.retry_after = retry_after


def problem(request, error: APIError):
    """Build an application/problem+json response with the request's correlation ID.

    Include Retry-After when supplied. The middleware attaches cache/security headers later;
    request_id is in-memory request context, not an automatic database record.
    """
    response = JsonResponse(
        {
            "type": f"urn:outage-explorer:error:{error.code}",
            "title": error.code.replace("_", " "),
            "status": error.status,
            "detail": error.detail,
            "code": error.code,
            "request_id": getattr(request, "request_id", ""),
        },
        status=error.status,
        content_type="application/problem+json",
    )
    if error.retry_after is not None:
        response["Retry-After"] = str(error.retry_after)
    return response
