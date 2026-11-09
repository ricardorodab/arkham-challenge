"""Exercise host secret retrieval without contacting Azure or reading real credentials."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import urllib.error
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "key_vault_secrets", Path(__file__).resolve().parents[1] / "deploy" / "key-vault-secrets.py"
)
secrets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(secrets)
SYNTHETIC_TOKEN = "synthetic-managed-identity-token"
SYNTHETIC_VALUE = "synthetic-secret-never-real"


class Response(io.BytesIO):
    status = 200

    def __init__(self, value):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())


def token_response(**changes):
    return {
        "access_token": SYNTHETIC_TOKEN,
        "token_type": "Bearer",
        "resource": "https://vault.azure.net",
        "expires_on": "10000",
        **changes,
    }


def secret_response(name="django-secret-key", **changes):
    return {
        "value": SYNTHETIC_VALUE,
        "id": f"{secrets.VAULT_URL}/secrets/{name}/" + "a" * 32,
        "attributes": {"enabled": True},
        **changes,
    }


def client_with(*responses):
    remaining = iter(responses)
    requests = []
    delays = []

    def open_request(request, timeout):
        requests.append((request, timeout))
        response = next(remaining)
        if isinstance(response, Exception):
            raise response
        return Response(response)

    client = secrets.VaultClient(
        SimpleNamespace(open=open_request), sleep=delays.append, clock=lambda: 1000
    )
    return client, requests, delays


def http_error(code):
    return urllib.error.HTTPError(
        secrets.VAULT_URL,
        code,
        SYNTHETIC_VALUE,
        {},
        io.BytesIO((SYNTHETIC_VALUE + SYNTHETIC_TOKEN).encode()),
    )


def test_fetch_uses_host_identity_once_and_never_lists_or_exports_token():
    client, requests, _ = client_with(
        token_response(), secret_response(), secret_response("eia-api-key")
    )
    django = client.secret("django-secret-key", required=True)
    eia = client.secret("eia-api-key", required=False)
    assert django["value"] == eia["value"] == SYNTHETIC_VALUE.encode()
    assert requests[0][0].full_url == secrets.TOKEN_URL
    assert requests[0][0].get_header("Metadata") == "true"
    assert len(requests) == 3
    for request, timeout in requests[1:]:
        assert request.full_url.startswith(secrets.VAULT_URL + "/secrets/")
        assert request.full_url.endswith("?api-version=" + secrets.API_VERSION)
        assert request.get_header("Authorization") == "Bearer " + SYNTHETIC_TOKEN
        assert timeout == 10


def test_default_transport_disables_proxy_and_redirects(monkeypatch):
    handlers = []
    monkeypatch.setattr(
        secrets.urllib.request, "build_opener", lambda *values: handlers.extend(values) or object()
    )
    secrets.VaultClient()
    assert any(isinstance(value, secrets.NoRedirect) for value in handlers)
    assert any(
        isinstance(value, secrets.urllib.request.ProxyHandler) and value.proxies == {}
        for value in handlers
    )
    with pytest.raises(secrets.SecretError, match="must not redirect") as caught:
        secrets.NoRedirect().redirect_request(None, None, 302, SYNTHETIC_TOKEN, {}, SYNTHETIC_VALUE)
    assert SYNTHETIC_TOKEN not in str(caught.value)
    assert SYNTHETIC_VALUE not in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"access_token": ""},
        {"access_token": "bad token"},
        {"token_type": "Basic"},
        {"token_type": None},
        {"resource": "https://management.azure.com/"},
        {"expires_on": "not-a-date"},
        {"expires_on": "1050"},
    ],
)
def test_invalid_or_wrong_audience_identity_tokens_fail_closed(changes):
    client, _, _ = client_with(token_response(**changes))
    with pytest.raises(secrets.SecretError, match="token response was invalid") as caught:
        client.secret("django-secret-key", required=True)
    assert SYNTHETIC_TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"value": ""},
        {"value": "  "},
        {"value": "bad\nsecret"},
        {"value": "bad\x00secret"},
        {"value": "s" * (secrets.MAX_SECRET + 1)},
        {"attributes": {"enabled": False}},
        {"attributes": {}},
        {"id": "https://attacker.invalid/secrets/django-secret-key/" + "a" * 32},
        {"id": secrets.VAULT_URL + "/secrets/eia-api-key/" + "a" * 32},
    ],
)
def test_invalid_secret_values_and_identifiers_never_materialize(changes):
    client, _, _ = client_with(token_response(), secret_response(**changes))
    with pytest.raises(secrets.SecretError, match="secret response was invalid"):
        client.secret("django-secret-key", required=True)


def test_optional_absence_is_distinct_from_missing_required_secret_or_denied_access():
    client, _, _ = client_with(token_response(), http_error(404))
    assert client.secret("eia-api-key", required=False) is None
    for code, required in [(404, True), (403, False), (401, False)]:
        client, _, _ = client_with(token_response(), http_error(code))
        with pytest.raises(secrets.SecretError, match="request was rejected") as caught:
            client.secret("django-secret-key", required=required)
        assert SYNTHETIC_VALUE not in str(caught.value)
        assert SYNTHETIC_TOKEN not in str(caught.value)


@pytest.mark.parametrize("attributes", [{"exp": 999}, {"nbf": 1001}, {"exp": "99999"}])
def test_expired_not_yet_valid_or_malformed_validity_dates_fail_closed(attributes):
    client, _, _ = client_with(
        token_response(), secret_response(attributes={"enabled": True, **attributes})
    )
    with pytest.raises(secrets.SecretError, match="validity period"):
        client.secret("django-secret-key", required=True)


def test_near_expiry_managed_identity_token_is_refreshed_in_memory():
    client, requests, _ = client_with(
        token_response(expires_on="1100"),
        secret_response(),
        token_response(access_token="synthetic-second-token", expires_on="10000"),
        secret_response("eia-api-key"),
    )
    client.secret("django-secret-key", required=True)
    client.clock = lambda: 1080
    client.secret("eia-api-key", required=False)
    assert len(requests) == 4
    assert requests[3][0].get_header("Authorization") == "Bearer synthetic-second-token"


def test_retry_is_bounded_and_hides_upstream_error_content():
    client, requests, delays = client_with(*[http_error(429) for _ in range(4)])
    with pytest.raises(secrets.SecretError, match="request was rejected") as caught:
        client.token()
    assert len(requests) == 4
    assert delays == [1, 2, 4]
    assert SYNTHETIC_VALUE not in str(caught.value)
    assert SYNTHETIC_TOKEN not in str(caught.value)


def test_transient_identity_failure_can_recover_without_partial_result():
    client, requests, delays = client_with(
        urllib.error.URLError(SYNTHETIC_VALUE), token_response(), secret_response()
    )
    assert client.secret("django-secret-key", required=True)["value"] == SYNTHETIC_VALUE.encode()
    assert len(requests) == 3 and delays == [1]


@pytest.mark.parametrize("body", [b"[]", b"invalid", b"x" * (secrets.MAX_RESPONSE + 1)])
def test_untrusted_response_json_is_bounded_and_has_safe_errors(body):
    client, _, _ = client_with(body)
    with pytest.raises(secrets.SecretError):
        client.token()


def test_only_fixed_secret_names_are_allowed_before_network_access():
    client, requests, _ = client_with()
    with pytest.raises(secrets.SecretError, match="unknown production secret"):
        client.secret("../unexpected", required=True)
    assert requests == []


def test_bootstrap_requirement_is_explicit_and_eia_can_remain_unconfigured():
    calls = []

    def secret(name, *, required):
        calls.append((name, required))
        return {"value": SYNTHETIC_VALUE.encode(), "version": "a" * 32} if required else None

    values = secrets.fetch_values({"bootstrap_required": False}, SimpleNamespace(secret=secret))
    assert set(values) == {"django-secret-key"}
    assert calls == [(name, name == "django-secret-key") for name in secrets.SECRET_NAMES]
    calls.clear()
    values = secrets.fetch_values({"bootstrap_required": True}, SimpleNamespace(secret=secret))
    assert "eia-api-key" not in values
    assert all(required for name, required in calls if name.startswith("bootstrap-"))


def test_configured_signing_key_pin_is_checked_without_exposing_the_key():
    def secret(name, *, required):
        return {"value": SYNTHETIC_VALUE.encode(), "version": "a" * 32}

    digest = hashlib.sha256(SYNTHETIC_VALUE.encode()).hexdigest()
    values = secrets.fetch_values(
        {"bootstrap_required": False, "django_sha256": digest}, SimpleNamespace(secret=secret)
    )
    assert values["django-secret-key"]["value"] == SYNTHETIC_VALUE.encode()
    with pytest.raises(secrets.SecretError, match="production identity pin") as caught:
        secrets.fetch_values(
            {"bootstrap_required": False, "django_sha256": "0" * 64},
            SimpleNamespace(secret=secret),
        )
    assert SYNTHETIC_VALUE not in str(caught.value)
    assert digest not in str(caught.value)


def test_root_validation_rejects_disk_storage_and_symlinked_runtime_path(monkeypatch, tmp_path):
    if os.name != "posix":
        pytest.skip("Host mount and ownership validation is Linux-only.")
    monkeypatch.setattr(secrets.os, "geteuid", lambda: 0)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("1 0 0:1 / / rw - ext4 /dev/root rw\n")
    with pytest.raises(secrets.SecretError, match="tmpfs"):
        secrets.validate_root(Path("/run/arkham-test-secrets"), mountinfo=mountinfo)
    with pytest.raises(secrets.SecretError, match="beneath /run"):
        secrets.validate_root(tmp_path, mountinfo=mountinfo)
    with pytest.raises(secrets.SecretError, match="beneath /run"):
        secrets.validate_root(Path("/run/../etc/unexpected"), mountinfo=mountinfo)
    monkeypatch.setattr(secrets.Path, "is_symlink", lambda self: self == Path("/run"))
    with pytest.raises(secrets.SecretError, match="symlinks"):
        secrets.validate_root(Path("/run/arkham-test-secrets"), mountinfo=mountinfo)


@pytest.fixture
def host_root(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("Linux ownership, permissions and bind-mount generation semantics.")
    monkeypatch.setattr(secrets, "HOST_UID", os.getuid())
    monkeypatch.setattr(secrets, "HOST_GID", os.getgid())
    monkeypatch.setattr(secrets, "RUNTIME_UID", os.getuid())
    monkeypatch.setattr(secrets, "RUNTIME_GID", os.getgid())
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    return root


def synthetic_values(*names, version="a" * 32):
    return {
        name: {"value": (SYNTHETIC_VALUE + name).encode(), "version": version} for name in names
    }


def test_generations_expose_only_role_files_and_never_secret_values_in_receipts(host_root):
    values = synthetic_values(*secrets.SECRET_NAMES)
    with secrets.generation_lock(host_root):
        receipt = secrets.materialize(host_root, values)
    generation = host_root / receipt["generation"]
    assert (host_root / "current").resolve() == generation
    assert set(receipt) == {"generation", "available", "changed"}
    assert SYNTHETIC_VALUE not in json.dumps(receipt)
    assert SYNTHETIC_VALUE not in (generation / "manifest.json").read_text()
    assert stat.S_IMODE(generation.stat().st_mode) == 0o700
    for role, filenames in {
        "app": {"django-secret-key"},
        "worker": {"django-secret-key", "eia-api-key"},
        "init": {"django-secret-key", "viewer-password", "analyst-password", "admin-password"},
    }.items():
        directory = generation / role
        assert {path.name for path in directory.iterdir()} == filenames
        assert stat.S_IMODE(directory.stat().st_mode) == 0o500
        for path in directory.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o400
            assert path.stat().st_uid == secrets.RUNTIME_UID


def test_missing_eia_does_not_create_placeholder_and_reuses_immutable_generation(host_root):
    values = synthetic_values("django-secret-key")
    first = secrets.materialize(host_root, values)
    current = host_root / "current"
    assert not (current / "worker" / "eia-api-key").exists()
    assert {path.name for path in (current / "worker").iterdir()} == {"django-secret-key"}
    second = secrets.materialize(host_root, values)
    assert second["generation"] == first["generation"]
    assert second["changed"] is False


def test_rotation_keeps_prior_generation_files_unchanged_for_existing_mounts(host_root):
    old_values = synthetic_values("django-secret-key")
    first = secrets.materialize(host_root, old_values)
    old_file = host_root / first["generation"] / "app" / "django-secret-key"
    old_inode = old_file.stat().st_ino
    new_values = synthetic_values("django-secret-key", "eia-api-key", version="b" * 32)
    new_values["django-secret-key"]["value"] = b"synthetic-new-signing-key"
    second = secrets.materialize(host_root, new_values)
    assert second["generation"] != first["generation"]
    assert old_file.read_bytes() == old_values["django-secret-key"]["value"]
    assert old_file.stat().st_ino == old_inode
    assert (
        host_root / "current" / "app" / "django-secret-key"
    ).read_bytes() == b"synthetic-new-signing-key"


def test_partial_materialization_failure_never_changes_current(host_root, monkeypatch):
    values = synthetic_values("django-secret-key")
    first = secrets.materialize(host_root, values)
    real_write = secrets._write
    calls = 0

    def fail_after_first_file(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic disk failure")
        real_write(*args, **kwargs)

    monkeypatch.setattr(secrets, "_write", fail_after_first_file)
    with pytest.raises(OSError, match="synthetic disk failure"):
        secrets.materialize(
            host_root, synthetic_values("django-secret-key", "eia-api-key", version="b" * 32)
        )
    assert (host_root / "current").resolve().name == first["generation"]
    assert not list(host_root.glob(".candidate-*"))


def test_signing_key_pin_mismatch_leaves_current_generation_unchanged(host_root):
    values = synthetic_values("django-secret-key")
    first = secrets.materialize(host_root, values)
    original = (host_root / "current" / "app" / "django-secret-key").read_bytes()

    def secret(name, *, required):
        return {"value": b"synthetic-unexpected-key", "version": "b" * 32} if required else None

    config = {
        "bootstrap_required": False,
        "django_sha256": hashlib.sha256(original).hexdigest(),
    }
    with (
        secrets.generation_lock(host_root),
        pytest.raises(secrets.SecretError, match="identity pin"),
    ):
        fetched = secrets.fetch_values(config, SimpleNamespace(secret=secret))
        secrets.materialize(host_root, fetched)
    assert (host_root / "current").resolve().name == first["generation"]
    assert (host_root / "current" / "app" / "django-secret-key").read_bytes() == original
    assert not list(host_root.glob(".candidate-*"))


def test_bad_reference_and_symlinked_secret_are_not_reused(host_root, tmp_path):
    (host_root / "current").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(secrets.SecretError, match="reference is invalid"):
        secrets.materialize(host_root, synthetic_values("django-secret-key"))
    (host_root / "current").unlink()
    first = secrets.materialize(host_root, synthetic_values("django-secret-key"))
    directory = host_root / first["generation"] / "app"
    os.chmod(directory, 0o700)
    (directory / "django-secret-key").unlink()
    (directory / "django-secret-key").symlink_to(tmp_path / "unexpected")
    os.chmod(directory, 0o500)
    next_receipt = secrets.materialize(host_root, synthetic_values("django-secret-key"))
    assert next_receipt["generation"] != first["generation"]
    assert not (host_root / "current" / "app" / "django-secret-key").is_symlink()


def test_config_rejects_other_vaults_unknown_fields_and_unsafe_permissions(host_root):
    path = host_root / "config.json"
    for changes in [
        {"vault_url": "https://unexpected.vault.azure.net"},
        {"token": "not-allowed"},
        {"django_sha256": "bad-pin"},
        {"django_sha256": None},
    ]:
        path.write_text(
            json.dumps({"vault_url": secrets.VAULT_URL, "bootstrap_required": False, **changes})
        )
        path.chmod(0o600)
        with pytest.raises(secrets.SecretError, match="configuration is invalid"):
            secrets.load_config(path)
    path.write_text(json.dumps({"vault_url": secrets.VAULT_URL, "bootstrap_required": False}))
    path.chmod(0o644)
    with pytest.raises(secrets.SecretError, match="protected secret-management file is invalid"):
        secrets.load_config(path)
    path.chmod(0o600)
    assert secrets.load_config(path) == {
        "vault_url": secrets.VAULT_URL,
        "bootstrap_required": False,
    }
    config = {
        "vault_url": secrets.VAULT_URL,
        "bootstrap_required": False,
        "django_sha256": "a" * 64,
    }
    path.write_text(json.dumps(config))
    assert secrets.load_config(path) == config


def test_fifo_cannot_block_protected_file_reader(host_root):
    path = host_root / "unexpected-fifo"
    os.mkfifo(path, mode=0o600)
    code = """import importlib.util,sys
spec=importlib.util.spec_from_file_location('protected_reader',sys.argv[1])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    module.regular_bytes(sys.argv[2],4096)
except module.SecretError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(SPEC.origin), str(path)],
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0


def test_cli_disables_core_dumps_before_reading_configuration_or_credentials(monkeypatch, tmp_path):
    calls = []
    resource = SimpleNamespace(
        RLIMIT_CORE=4, setrlimit=lambda kind, limits: calls.append(("core", kind, limits))
    )
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(sys, "argv", ["key-vault-secrets.py", "fetch"])
    monkeypatch.setattr(secrets, "validate_root", lambda root: tmp_path)
    monkeypatch.setattr(secrets, "generation_lock", lambda root: nullcontext())
    monkeypatch.setattr(secrets, "VaultClient", lambda: object())

    def configuration(path):
        calls.append(("config",))
        return {"bootstrap_required": False}

    def fetch(config, client):
        calls.append(("fetch",))
        return synthetic_values("django-secret-key")

    monkeypatch.setattr(secrets, "load_config", configuration)
    monkeypatch.setattr(secrets, "fetch_values", fetch)
    monkeypatch.setattr(secrets, "materialize", lambda root, values: {"generation": "synthetic"})
    secrets.main()
    assert calls == [("core", 4, (0, 0)), ("config",), ("fetch",)]
