"""Verify credential-free public liveness checks fail on unsafe or unhealthy responses."""

from __future__ import annotations

import importlib.util
import io
import json
import ssl
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("public_health", ROOT / "deploy" / "check-health.py")
health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health)
HOST = "synthetic.example"
URL = "https://" + HOST + "/api/v1/health/live"


class Response:
    def __init__(self, *, status=200, body=b'{"status":"ok"}', revision="a" * 40, hsts=None):
        self.status = status
        self.body = body
        self.headers = {"X-Outage-Revision": revision}
        if hsts is not None:
            self.headers["Strict-Transport-Security"] = hsts
        self.url = URL
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def geturl(self):
        return self.url

    def read(self, limit):
        self.read_limit = limit
        return self.body[:limit]


def transport(monkeypatch, response=None, error=None):
    observed = {}

    def open_request(request, *, timeout):
        observed.update(request=request, timeout=timeout)
        if error is not None:
            raise error
        return response

    def build_opener(*handlers):
        observed["handlers"] = handlers
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(health.urllib.request, "build_opener", build_opener)
    return observed


@pytest.mark.parametrize("revision", ["a" * 40, "B" * 64])
def test_live_service_accepts_valid_older_revision_without_credentials(monkeypatch, revision):
    response = Response(revision=revision, hsts="max-age=31536000; includeSubDomains")
    observed = transport(monkeypatch, response)
    receipt = health.check({"OUTAGE_PUBLIC_HOST": HOST, "GITHUB_SHA": "c" * 40, "EIA_API_KEY": ""})
    assert receipt == {
        "health": "ok",
        "host": HOST,
        "revision": revision.lower(),
        "https": True,
    }
    assert observed["request"].full_url == URL
    assert "Authorization" not in observed["request"].headers
    assert "Cookie" not in observed["request"].headers
    assert observed["timeout"] == health.TIMEOUT_SECONDS
    assert response.read_limit == health.MAX_BODY_BYTES + 1
    assert any(
        isinstance(handler, health.urllib.request.ProxyHandler) and handler.proxies == {}
        for handler in observed["handlers"]
    )
    assert any(isinstance(handler, health.NoRedirect) for handler in observed["handlers"])


@pytest.mark.parametrize(
    "host",
    [
        "",
        "https://synthetic.example",
        "user@synthetic.example",
        "synthetic.example:443",
        "synthetic.example/path",
        "synthetic.example\r\nInjected: yes",
        " localhost",
        "localhost",
        "a" * 64 + ".example",
        "a." * 126 + "example",
    ],
)
def test_invalid_host_never_reaches_the_network(monkeypatch, host):
    monkeypatch.setattr(
        health.urllib.request,
        "build_opener",
        lambda *_: pytest.fail("Invalid host reached network"),
    )
    with pytest.raises(health.HealthCheckError, match="DNS hostname"):
        health.check({"OUTAGE_PUBLIC_HOST": host})


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("synthetic transport detail must not be echoed"),
        TimeoutError("synthetic timeout must not be echoed"),
        ssl.SSLCertVerificationError("synthetic certificate detail must not be echoed"),
    ],
)
def test_transport_and_certificate_failures_are_safe_failures(monkeypatch, error):
    transport(monkeypatch, error=error)
    with pytest.raises(health.HealthCheckError, match="request failed") as failure:
        health.check({"OUTAGE_PUBLIC_HOST": HOST})
    assert "synthetic" not in str(failure.value)


@pytest.mark.parametrize("status", [301, 302, 401, 403, 500, 503])
def test_http_failure_or_redirect_is_rejected_without_remote_diagnostics(monkeypatch, status):
    detail = "remote-response-must-not-be-printed"
    error = urllib.error.HTTPError(URL, status, detail, {}, io.BytesIO(detail.encode()))
    transport(monkeypatch, error=error)
    with pytest.raises(health.HealthCheckError, match=f"HTTP {status}") as failure:
        health.check({"OUTAGE_PUBLIC_HOST": HOST})
    assert detail not in str(failure.value)


def test_redirect_handler_refuses_even_an_https_redirect():
    handler = health.NoRedirect()
    request = health.urllib.request.Request(URL)
    assert handler.redirect_request(request, None, 302, "redirect", {}, URL + "/other") is None


@pytest.mark.parametrize(
    "options,message",
    [
        ({"status": 204}, "HTTP 200"),
        ({"body": b"x" * (health.MAX_BODY_BYTES + 1)}, "oversized"),
        ({"body": b"not-json"}, "invalid JSON"),
        ({"body": b"\xff"}, "invalid JSON"),
        ({"body": b'{"status":"starting"}'}, "healthy status"),
        ({"body": b'{"status":"ok","dataset":"missing"}'}, "healthy status"),
        ({"revision": None}, "deployed revision"),
        ({"revision": "a" * 39}, "deployed revision"),
        ({"revision": "g" * 40}, "deployed revision"),
        ({"hsts": None}, "HSTS"),
        ({"hsts": "max-age=0"}, "HSTS"),
        ({"hsts": "max-age=invalid"}, "HSTS"),
        ({"hsts": "max-age=31536000; max-age=0"}, "HSTS"),
    ],
)
def test_unhealthy_or_unverifiable_success_response_fails(monkeypatch, options, message):
    configuration = {"hsts": "max-age=31536000", **options}
    transport(monkeypatch, Response(**configuration))
    with pytest.raises(health.HealthCheckError, match=message):
        health.check({"OUTAGE_PUBLIC_HOST": HOST})


def test_changed_final_origin_is_rejected(monkeypatch):
    response = Response(hsts="max-age=31536000")
    response.url = "https://other.example/api/v1/health/live"
    transport(monkeypatch, response)
    with pytest.raises(health.HealthCheckError, match="exact origin"):
        health.check({"OUTAGE_PUBLIC_HOST": HOST})


def test_command_reports_safe_failure_and_nonzero_status(monkeypatch, capsys):
    monkeypatch.setattr(
        health,
        "check",
        lambda *_: (_ for _ in ()).throw(health.HealthCheckError("Synthetic safe failure.")),
    )
    assert health.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"health": "failed", "error": "Synthetic safe failure."}
