"""Verify the dedicated public VM image using a complete anonymous GHCR pull flow.

The anonymous registry token stays in memory. This gate requires a successful token
exchange, the exact immutable manifest, and its matching Docker-Content-Digest header.
No authenticated package metadata or registry credential is needed by the VM.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json,"
    "application/vnd.oci.image.manifest.v1+json,"
    "application/vnd.docker.distribution.manifest.list.v2+json,"
    "application/vnd.docker.distribution.manifest.v2+json"
)


class VerificationError(RuntimeError):
    """Expose only deliberate messages containing no tokens or remote response bodies."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Fail closed instead of forwarding an authorization header to a redirected origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, headers):
    """Read a bounded response with no cookies, proxy credentials or unexpected redirects."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(
        url, headers={"User-Agent": "Arkham-package-verifier", **headers}
    )
    try:
        with opener.open(request, timeout=30) as response:
            status = response.status
            body = response.read(1_048_577)
            response_headers = {name.lower(): value for name, value in response.headers.items()}
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return status, b"", {}
    except (OSError, ValueError):
        raise VerificationError(
            "Package verification could not complete its HTTPS request."
        ) from None
    if len(body) > 1_048_576:
        raise VerificationError("Package verification received an oversized response.")
    return status, body, response_headers


def document(body):
    """Parse a JSON object without including remote content in failures."""
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        raise VerificationError("Package verification received an invalid JSON response.") from None
    if not isinstance(value, dict):
        raise VerificationError("Package verification received an unexpected JSON response.")
    return value


def verify_anonymous_pull(image_path, digest):
    """Exchange an anonymous token and verify the exact public manifest digest."""
    query = urllib.parse.urlencode(
        {"service": "ghcr.io", "scope": "repository:" + image_path + ":pull"}
    )
    status, body, _ = fetch("https://ghcr.io/token?" + query, {})
    if status != 200:
        raise VerificationError(f"Anonymous registry token request failed (HTTP {status}).")
    token = document(body).get("token")
    if not isinstance(token, str) or not token:
        raise VerificationError("The anonymous registry check did not return a usable token.")
    status, _, headers = fetch(
        "https://ghcr.io/v2/" + image_path + "/manifests/" + digest,
        {"Authorization": "Bearer " + token, "Accept": MANIFEST_ACCEPT},
    )
    if status != 200:
        raise VerificationError(f"Anonymous manifest request failed (HTTP {status}).")
    if headers.get("docker-content-digest") != digest:
        raise VerificationError("The public manifest did not report the exact accepted digest.")


def verify(environment):
    """Require this repository's dedicated public image and exact accepted digest."""
    repository = environment.get("GITHUB_REPOSITORY", "")
    image = environment.get("OUTAGE_IMAGE", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
        raise VerificationError("Required package-verification inputs are missing or invalid.")
    image_path = repository.lower() + "-vm"
    prefix = "ghcr.io/" + image_path + "@"
    if not re.fullmatch(re.escape(prefix) + r"sha256:[0-9a-f]{64}", image):
        raise VerificationError(
            "The package verifier requires this repository's exact VM image digest."
        )
    verify_anonymous_pull(image_path, image[len(prefix) :])
    return {"package": "public", "image": image, "anonymous_pull": "verified"}


def main():
    """Emit public verification facts and static failures, never response bodies or tokens."""
    try:
        receipt = verify(os.environ)
    except VerificationError as error:
        print(json.dumps({"package": "unverified", "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
