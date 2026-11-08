"""Container binding is explicit; ordinary local startup stays on loopback."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from django.core.handlers.wsgi import WSGIHandler
from django.test import Client, RequestFactory
from waitress.proxy_headers import proxy_headers_middleware

from outage_explorer.web import serve


@pytest.mark.parametrize(
    "arguments,expected_host", [([], "127.0.0.1"), (["--host", "0.0.0.0"], "0.0.0.0")]
)
def test_waitress_uses_explicit_bind_address_and_preserves_local_default(
    monkeypatch, arguments, expected_host
):
    application = object()
    monkeypatch.delenv("OUTAGE_TRUSTED_PROXY", raising=False)
    monkeypatch.setitem(
        sys.modules, "outage_explorer.web.wsgi", SimpleNamespace(application=application)
    )
    observed = {}

    def start(wsgi, **options):
        assert wsgi is application
        observed.update(options)

    monkeypatch.setattr(serve, "serve", start)
    monkeypatch.setattr(sys, "argv", ["serve", *arguments, "--port", "8123"])
    serve.main()
    assert observed["host"] == expected_host
    assert observed["port"] == 8123
    assert "trusted_proxy" not in observed
    assert "trusted_proxy_headers" not in observed
    assert observed["clear_untrusted_proxy_headers"] is True


def test_waitress_trusts_only_the_configured_proxy_and_forwarded_scheme(monkeypatch):
    application = object()
    monkeypatch.setitem(
        sys.modules, "outage_explorer.web.wsgi", SimpleNamespace(application=application)
    )
    monkeypatch.setenv("OUTAGE_TRUSTED_PROXY", "172.30.89.2")
    observed = {}
    monkeypatch.setattr(serve, "serve", lambda _app, **options: observed.update(options))
    monkeypatch.setattr(sys, "argv", ["serve"])
    serve.main()
    assert observed["trusted_proxy"] == "172.30.89.2"
    assert observed["trusted_proxy_count"] == 1
    assert observed["trusted_proxy_headers"] == {"x-forwarded-proto"}
    assert observed["clear_untrusted_proxy_headers"] is True


@pytest.mark.parametrize("proxy", ["*", "proxy.example", "172.30.89.2/24", "127.0.0.1:8000"])
def test_invalid_trusted_proxy_is_rejected_before_starting_waitress(monkeypatch, proxy):
    monkeypatch.setenv("OUTAGE_TRUSTED_PROXY", proxy)
    monkeypatch.setattr(sys, "argv", ["serve"])
    monkeypatch.setattr(serve, "serve", lambda *_args, **_kwargs: pytest.fail("Server started"))
    with pytest.raises(SystemExit) as error:
        serve.main()
    assert error.value.code == 2


@pytest.mark.parametrize("peer,trusted", [("172.30.89.2", True), ("172.30.89.3", False)])
@pytest.mark.django_db
def test_https_proxy_translation_preserves_host_and_rejects_spoofed_headers(
    settings, peer, trusted
):
    settings.ALLOWED_HOSTS = ["arkham.example"]
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_PROXY_SSL_HEADER = None
    settings.CSRF_COOKIE_SECURE = True
    environ = (
        RequestFactory()
        .get(
            "/api/v1/auth/csrf",
            HTTP_HOST="arkham.example",
            REMOTE_ADDR=peer,
            HTTP_X_FORWARDED_PROTO="https",
            HTTP_X_FORWARDED_HOST="attacker.example",
            HTTP_X_FORWARDED_FOR="192.0.2.1",
            HTTP_FORWARDED="for=192.0.2.1;host=attacker.example;proto=https",
        )
        .environ
    )
    application = proxy_headers_middleware(
        WSGIHandler(),
        trusted_proxy="172.30.89.2",
        trusted_proxy_headers={"x-forwarded-proto"},
        clear_untrusted=True,
    )
    observed = {}

    def start_response(status, headers):
        observed.update(status=status, headers=dict(headers))

    result = application(environ, start_response)
    try:
        b"".join(result)
    finally:
        result.close()
    assert environ["HTTP_HOST"] == "arkham.example"
    assert environ["REMOTE_ADDR"] == peer
    assert "HTTP_FORWARDED" not in environ
    assert "HTTP_X_FORWARDED_HOST" not in environ
    assert "HTTP_X_FORWARDED_FOR" not in environ
    if trusted:
        assert observed["status"].startswith("200 ")
        assert environ["wsgi.url_scheme"] == "https"
        assert "; Secure" in observed["headers"]["Set-Cookie"]
    else:
        assert observed["status"].startswith("301 ")
        assert environ["wsgi.url_scheme"] == "http"
        assert "HTTP_X_FORWARDED_PROTO" not in environ
        assert observed["headers"]["Location"] == "https://arkham.example/api/v1/auth/csrf"


@pytest.mark.parametrize(
    "path,status",
    [
        ("/api/v1/health/live", 200),
        ("/api/v1/health/live/", 301),
        ("/api/v1/health/live/extra", 301),
        ("/api/v1/auth/login", 301),
    ],
)
@pytest.mark.django_db
def test_http_redirect_exemption_is_limited_to_exact_public_liveness(settings, path, status):
    settings.SECURE_SSL_REDIRECT = True
    settings.SECURE_PROXY_SSL_HEADER = None
    result = Client().get(path, HTTP_X_FORWARDED_PROTO="https")
    assert result.status_code == status
    if status == 200:
        assert result.json() == {"status": "ok"}
        assert not result.cookies
        assert result["Cache-Control"] == "no-store"
    else:
        assert result["Location"] == "https://testserver" + path


def test_invalid_bind_address_is_rejected_before_starting_waitress(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["serve", "--host", "invalid-address"])
    with pytest.raises(SystemExit) as error:
        serve.main()
    assert error.value.code == 2
