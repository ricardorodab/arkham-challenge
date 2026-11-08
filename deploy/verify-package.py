"""Require the dedicated VM image to be private and associated with this repository.

The GitHub Actions job token and any anonymous registry token remain in memory. A bare
registry 401 is insufficient: public GHCR images also require an anonymous bearer-token
exchange. This gate checks authenticated package metadata and that full anonymous flow.
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
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        return status, b""
    except (OSError, ValueError):
        raise VerificationError(
            "Package verification could not complete its HTTPS request."
        ) from None
    if len(body) > 1_048_576:
        raise VerificationError("Package verification received an oversized response.")
    return status, body


def document(body):
    """Parse a JSON object without including remote content in failures."""
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        raise VerificationError("Package verification received an invalid JSON response.") from None
    if not isinstance(value, dict):
        raise VerificationError("Package verification received an unexpected JSON response.")
    return value


def github(path, token):
    """Use the job's installation token only with the fixed GitHub API origin."""
    status, body = fetch(
        "https://api.github.com/" + path,
        {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    if status != 200:
        phase = "repository" if path.startswith("repos/") else "package"
        raise VerificationError(f"Authenticated {phase} metadata was unavailable (HTTP {status}).")
    return document(body)


def anonymous_pull_denied(image_path, digest):
    """Exchange only anonymous credentials and reject a publicly retrievable manifest."""
    query = urllib.parse.urlencode(
        {"service": "ghcr.io", "scope": "repository:" + image_path + ":pull"}
    )
    status, body = fetch("https://ghcr.io/token?" + query, {})
    if status in {401, 403}:
        return
    if status != 200:
        raise VerificationError("The anonymous registry check did not return a valid denial.")
    token = document(body).get("token")
    if not isinstance(token, str) or not token:
        raise VerificationError("The anonymous registry check did not return a usable token.")
    status, _ = fetch(
        "https://ghcr.io/v2/" + image_path + "/manifests/" + digest,
        {"Authorization": "Bearer " + token, "Accept": MANIFEST_ACCEPT},
    )
    if status not in {401, 403, 404}:
        raise VerificationError(
            "The VM image was anonymously accessible or privacy was unverified."
        )


def verify(environment):
    """Require private metadata, the exact source repository, and denied anonymous pull."""
    repository = environment.get("GITHUB_REPOSITORY", "")
    repository_id = environment.get("GITHUB_REPOSITORY_ID", "")
    token = environment.get("GHCR_TOKEN", "")
    image = environment.get("OUTAGE_IMAGE", "")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository)
        or not re.fullmatch(r"[0-9]+", repository_id)
        or not token
    ):
        raise VerificationError("Required package-verification inputs are missing or invalid.")
    image_path = repository.lower() + "-vm"
    prefix = "ghcr.io/" + image_path + "@"
    if not re.fullmatch(re.escape(prefix) + r"sha256:[0-9a-f]{64}", image):
        raise VerificationError(
            "The package verifier requires this repository's exact VM image digest."
        )
    source = github("repos/" + repository, token)
    owner = source.get("owner", {})
    if (
        not isinstance(owner, dict)
        or source.get("id") != int(repository_id)
        or str(source.get("full_name", "")).lower() != repository.lower()
        or str(owner.get("login", "")).lower() != repository.split("/")[0].lower()
        or owner.get("type") not in {"User", "Organization"}
        or type(owner.get("id")) is not int
    ):
        raise VerificationError("The package source repository could not be verified.")
    owner_route = "orgs" if owner["type"] == "Organization" else "users"
    package_name = repository.split("/")[1].lower() + "-vm"
    package = github(
        owner_route + "/" + owner["login"] + "/packages/container/" + package_name, token
    )
    package_owner = package.get("owner", {})
    package_repository = package.get("repository", {})
    if (
        not isinstance(package_owner, dict)
        or not isinstance(package_repository, dict)
        or package.get("visibility") != "private"
        or package.get("package_type") != "container"
        or package.get("name") != package_name
        or package_repository.get("id") != int(repository_id)
        or package_owner.get("id") != owner["id"]
        or str(package_owner.get("login", "")).lower() != str(owner["login"]).lower()
    ):
        raise VerificationError(
            "The VM package must be private and linked to this exact repository."
        )
    anonymous_pull_denied(image_path, image[len(prefix) :])
    return {"package": "private", "image": image, "anonymous_pull": "denied"}


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
