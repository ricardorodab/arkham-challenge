"""Container binding is explicit; ordinary local startup stays on loopback."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from outage_explorer.web import serve


@pytest.mark.parametrize(
    "arguments,expected_host", [([], "127.0.0.1"), (["--host", "0.0.0.0"], "0.0.0.0")]
)
def test_waitress_uses_explicit_bind_address_and_preserves_local_default(
    monkeypatch, arguments, expected_host
):
    application = object()
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


def test_invalid_bind_address_is_rejected_before_starting_waitress(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["serve", "--host", "invalid-address"])
    with pytest.raises(SystemExit) as error:
        serve.main()
    assert error.value.code == 2
