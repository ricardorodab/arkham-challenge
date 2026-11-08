"""Check anonymous registry-token exchange and exact immutable public-image verification."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_package", ROOT / "deploy" / "verify-package.py"
)
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)
DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def environment():
    return {
        "GITHUB_REPOSITORY": "example/repository",
        "OUTAGE_IMAGE": "ghcr.io/example/repository-vm@" + DIGEST,
    }


def test_public_image_verifies_full_anonymous_flow_without_job_credentials(
    monkeypatch, environment
):
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        assert not url.startswith("https://api.github.com/")
        if "/token?" in url:
            assert headers == {}
            assert "repository%3Aexample%2Frepository-vm%3Apull" in url
            return 200, b'{"token":"synthetic-anonymous-token"}', {}
        assert url == "https://ghcr.io/v2/example/repository-vm/manifests/" + DIGEST
        assert headers["Authorization"] == "Bearer synthetic-anonymous-token"
        assert "application/vnd.oci.image.manifest.v1+json" in headers["Accept"]
        return 200, b"synthetic-manifest", {"docker-content-digest": DIGEST}

    monkeypatch.setattr(package, "fetch", fetch)
    assert package.verify(environment) == {
        "package": "public",
        "image": environment["OUTAGE_IMAGE"],
        "anonymous_pull": "verified",
    }
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 302])
def test_manifest_denial_or_ambiguous_status_blocks_credential_free_deployment(monkeypatch, status):
    responses = iter([(200, b'{"token":"synthetic-anonymous-token"}', {}), (status, b"", {})])
    monkeypatch.setattr(package, "fetch", lambda *_: next(responses))
    with pytest.raises(package.VerificationError, match=f"HTTP {status}"):
        package.verify_anonymous_pull("example/repository-vm", DIGEST)


@pytest.mark.parametrize("reported_digest", [None, "sha256:" + "b" * 64])
def test_manifest_must_report_the_exact_accepted_digest(monkeypatch, reported_digest):
    responses = iter(
        [
            (200, b'{"token":"synthetic-anonymous-token"}', {}),
            (200, b"manifest", {"docker-content-digest": reported_digest}),
        ]
    )
    monkeypatch.setattr(package, "fetch", lambda *_: next(responses))
    with pytest.raises(package.VerificationError, match="exact accepted digest"):
        package.verify_anonymous_pull("example/repository-vm", DIGEST)


@pytest.mark.parametrize(
    "status,body",
    [(401, b""), (403, b""), (429, b""), (500, b""), (200, b"{}"), (200, b"not JSON")],
)
def test_token_service_failure_or_malformed_response_cannot_pass(monkeypatch, status, body):
    monkeypatch.setattr(package, "fetch", lambda *_: (status, body, {}))
    with pytest.raises(package.VerificationError):
        package.verify_anonymous_pull("example/repository-vm", DIGEST)


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/example/repository-vm:latest",
        "ghcr.io/example/repository@" + DIGEST,
        "ghcr.io/another/repository-vm@" + DIGEST,
    ],
)
def test_only_the_dedicated_repository_image_digest_is_accepted(monkeypatch, environment, image):
    monkeypatch.setattr(package, "fetch", lambda *_: pytest.fail("Invalid image reached registry"))
    with pytest.raises(package.VerificationError):
        package.verify({**environment, "OUTAGE_IMAGE": image})


def test_transport_errors_never_reveal_an_anonymous_authorization_header(monkeypatch, capsys):
    token = "synthetic-anonymous-token"

    def fail(*_args, **_kwargs):
        raise ValueError("Synthetic invalid Authorization header: Bearer " + token)

    monkeypatch.setattr(
        package.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=fail)
    )
    with pytest.raises(package.VerificationError) as error:
        package.fetch("https://ghcr.io/synthetic", {"Authorization": "Bearer " + token})
    assert token not in str(error.value)
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


def test_manifest_digest_header_is_normalized_case_insensitively(monkeypatch):
    class Response:
        status = 200
        headers = {"Docker-Content-Digest": DIGEST}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _):
            return b"manifest"

    monkeypatch.setattr(
        package.urllib.request,
        "build_opener",
        lambda *_: SimpleNamespace(open=lambda *_args, **_kwargs: Response()),
    )
    status, body, headers = package.fetch("https://ghcr.io/synthetic", {})
    assert status == 200 and body == b"manifest"
    assert headers["docker-content-digest"] == DIGEST
