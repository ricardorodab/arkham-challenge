"""Check public HTTPS process liveness without credentials or deployment side effects.

Any valid deployed revision is accepted while a newer commit is still passing CI. This
check does not assess analytical readiness, require an EIA key, restart services, or deploy.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

MAX_BODY_BYTES = 1024
TIMEOUT_SECONDS = 15


class HealthCheckError(RuntimeError):
    """Contain deliberate operational messages, never remote response bodies."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Require the configured HTTPS origin instead of following a redirected response."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_host(environment):
    """Accept one explicit DNS hostname, with no scheme, port, path or credentials."""
    host = environment.get("OUTAGE_PUBLIC_HOST", "")
    pattern = (
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    )
    if not isinstance(host, str) or len(host) > 253 or not re.fullmatch(pattern, host):
        raise HealthCheckError("OUTAGE_PUBLIC_HOST must be an explicit public DNS hostname.")
    return host


def hsts_enabled(value):
    """Require one valid positive HSTS max-age; optional other directives are allowed."""
    if not isinstance(value, str):
        return False
    ages = []
    for directive in value.split(";"):
        directive = directive.strip()
        if directive.lower().startswith("max-age"):
            match = re.fullmatch(r"max-age\s*=\s*([0-9]+)", directive, flags=re.IGNORECASE)
            if match is None:
                return False
            ages.append(match[1])
    return len(ages) == 1 and any(character != "0" for character in ages[0])


def check(environment):
    """Verify bounded live-health JSON, a deployed revision, HSTS and certificate-valid TLS."""
    host = public_host(environment)
    url = "https://" + host + "/api/v1/health/live"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(
        url,
        headers={"Cache-Control": "no-cache", "User-Agent": "Arkham-availability-check"},
    )
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
            final_url = response.geturl()
            body = response.read(MAX_BODY_BYTES + 1)
            revision = response.headers.get("X-Outage-Revision")
            hsts = response.headers.get("Strict-Transport-Security")
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise HealthCheckError(f"Public HTTPS liveness returned HTTP {status}.") from None
    except (OSError, ValueError):
        raise HealthCheckError("Public HTTPS liveness request failed.") from None
    if status != 200 or final_url != url:
        raise HealthCheckError("Public HTTPS liveness did not return HTTP 200 at its exact origin.")
    if len(body) > MAX_BODY_BYTES:
        raise HealthCheckError("Public HTTPS liveness returned an oversized response.")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        raise HealthCheckError("Public HTTPS liveness returned invalid JSON.") from None
    if payload != {"status": "ok"}:
        raise HealthCheckError("Public HTTPS liveness did not report the expected healthy status.")
    if not isinstance(revision, str) or not re.fullmatch(
        r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", revision
    ):
        raise HealthCheckError("Public HTTPS liveness did not report a valid deployed revision.")
    if not hsts_enabled(hsts):
        raise HealthCheckError("Public HTTPS liveness did not report an active HSTS policy.")
    return {"health": "ok", "host": host, "revision": revision.lower(), "https": True}


def main():
    """Emit only a public health receipt or a safe failure, with an appropriate exit code."""
    try:
        receipt = check(os.environ)
    except HealthCheckError as error:
        print(json.dumps({"health": "failed", "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
