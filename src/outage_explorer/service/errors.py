"""Public errors never include SQL, credentials, paths or engine diagnostics."""

from __future__ import annotations

from django.http import JsonResponse


class APIError(Exception):
    def __init__(self, status: int, code: str, detail: str, *, retry_after: int | None = None):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail
        self.retry_after = retry_after


def problem(request, error: APIError):
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
