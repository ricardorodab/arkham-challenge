"""Check package privacy using authenticated metadata and a real anonymous-token flow."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_package", ROOT / "deploy" / "verify-package.py"
)
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


@pytest.fixture
def environment():
    return {
        "GITHUB_REPOSITORY": "example/repository",
        "GITHUB_REPOSITORY_ID": "12345",
        "GHCR_TOKEN": "synthetic-job-token-never-use",
        "OUTAGE_IMAGE": "ghcr.io/example/repository-vm@sha256:" + "a" * 64,
    }


def metadata(owner_type="User"):
    owner = {"login": "example", "id": 6789, "type": owner_type}
    source = {"id": 12345, "full_name": "example/repository", "owner": owner}
    image = {
        "name": "repository-vm",
        "package_type": "container",
        "visibility": "private",
        "repository": {"id": 12345},
        "owner": owner,
    }
    return source, image


@pytest.mark.parametrize("owner_type,owner_route", [("User", "users"), ("Organization", "orgs")])
def test_private_package_requires_metadata_and_denied_anonymous_pull(
    monkeypatch, environment, owner_type, owner_route
):
    source, image = metadata(owner_type)
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        if url == "https://api.github.com/repos/example/repository":
            assert headers["Authorization"] == "Bearer " + environment["GHCR_TOKEN"]
            return 200, json.dumps(source).encode()
        if url == f"https://api.github.com/{owner_route}/example/packages/container/repository-vm":
            assert headers["Authorization"] == "Bearer " + environment["GHCR_TOKEN"]
            return 200, json.dumps(image).encode()
        assert url.startswith("https://ghcr.io/token?")
        assert "Authorization" not in headers
        assert environment["GHCR_TOKEN"] not in str(headers)
        return 403, b""

    monkeypatch.setattr(package, "fetch", fetch)
    assert package.verify(environment) == {
        "package": "private",
        "image": environment["OUTAGE_IMAGE"],
        "anonymous_pull": "denied",
    }
    assert len(calls) == 3


@pytest.mark.parametrize(
    "changed",
    [
        {"visibility": "public"},
        {"visibility": "internal"},
        {"repository": {"id": 54321}},
        {"repository": None},
        {"owner": {"id": 9876, "login": "someone-else"}},
        {"name": "repository"},
        {"package_type": "npm"},
    ],
)
def test_public_or_unassociated_package_is_never_promoted(monkeypatch, environment, changed):
    source, image = metadata()
    image.update(changed)
    responses = iter([source, image])
    monkeypatch.setattr(package, "github", lambda *_: next(responses))
    monkeypatch.setattr(
        package, "anonymous_pull_denied", lambda *_: pytest.fail("Metadata failed open")
    )
    with pytest.raises(package.VerificationError):
        package.verify(environment)


@pytest.mark.parametrize("status", [200, 429, 500, 302])
def test_anonymous_bearer_manifest_success_or_ambiguous_status_fails(monkeypatch, status):
    calls = []

    def fetch(url, headers):
        calls.append(url)
        if "/token?" in url:
            assert headers == {}
            return 200, b'{"token":"synthetic-anonymous-token"}'
        assert "/manifests/sha256:" in url
        assert headers["Authorization"] == "Bearer synthetic-anonymous-token"
        assert "application/vnd.oci.image.manifest.v1+json" in headers["Accept"]
        return status, b""

    monkeypatch.setattr(package, "fetch", fetch)
    with pytest.raises(package.VerificationError):
        package.anonymous_pull_denied("example/repository-vm", "sha256:" + "a" * 64)
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 403, 404])
def test_anonymous_bearer_manifest_denial_is_accepted(monkeypatch, status):
    responses = iter([(200, b'{"token":"synthetic-anonymous-token"}'), (status, b"")])
    monkeypatch.setattr(package, "fetch", lambda *_: next(responses))
    package.anonymous_pull_denied("example/repository-vm", "sha256:" + "a" * 64)


@pytest.mark.parametrize("status,body", [(429, b""), (500, b""), (200, b"{}"), (200, b"not JSON")])
def test_token_service_outage_or_malformed_response_cannot_count_as_privacy(
    monkeypatch, status, body
):
    monkeypatch.setattr(package, "fetch", lambda *_: (status, body))
    with pytest.raises(package.VerificationError):
        package.anonymous_pull_denied("example/repository-vm", "sha256:" + "a" * 64)


@pytest.mark.parametrize(
    "path,phase",
    [
        ("repos/example/repository", "repository"),
        ("users/example/packages/container/repository-vm", "package"),
    ],
)
def test_metadata_access_failure_identifies_phase_without_response_or_token(
    monkeypatch, environment, capsys, path, phase
):
    token = environment["GHCR_TOKEN"]
    monkeypatch.setattr(package, "fetch", lambda *_: (403, token.encode()))
    with pytest.raises(package.VerificationError) as error:
        package.github(path, token)
    assert f"Authenticated {phase} metadata" in str(error.value)
    assert "HTTP 403" in str(error.value)
    assert token not in str(error.value)
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


def test_transport_errors_never_reveal_an_authorization_header(monkeypatch, environment, capsys):
    token = environment["GHCR_TOKEN"]

    def fail(*_args, **_kwargs):
        raise ValueError("Synthetic invalid Authorization header: Bearer " + token)

    monkeypatch.setattr(
        package.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=fail)
    )
    with pytest.raises(package.VerificationError) as error:
        package.github("repos/example/repository", token)
    assert token not in str(error.value)
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
