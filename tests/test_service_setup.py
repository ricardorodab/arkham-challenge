"""Secure configuration and explicit, repeatable demo-account setup."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from outage_explorer.service.models import Persona


def demo_passwords(monkeypatch, suffix="A9!synthetic-only"):
    for role in Persona.Role.values:
        monkeypatch.setenv(f"OUTAGE_{role.upper()}_PASSWORD", f"Example-{role}-{suffix}")


@pytest.mark.django_db
def test_seed_requires_passwords_and_changes_nothing_on_missing_configuration(monkeypatch):
    for role in Persona.Role.values:
        monkeypatch.delenv(f"OUTAGE_{role.upper()}_PASSWORD", raising=False)
    with pytest.raises(CommandError, match="PASSWORD is required"):
        call_command("seed_users")
    assert get_user_model().objects.count() == 0


@pytest.mark.django_db
def test_seed_assigns_one_persona_without_django_staff_privileges(monkeypatch):
    demo_passwords(monkeypatch)
    call_command("seed_users")
    assert set(get_user_model().objects.values_list("username", flat=True)) == set(
        Persona.Role.values
    )
    assert Persona.objects.count() == 3
    for user in get_user_model().objects.all():
        assert user.persona.role == user.username
        assert user.is_active and not user.is_staff and not user.is_superuser
        assert user.check_password(os.environ[f"OUTAGE_{user.username.upper()}_PASSWORD"])


@pytest.mark.django_db
def test_seed_preserves_passwords_until_explicit_reset(monkeypatch):
    demo_passwords(monkeypatch)
    call_command("seed_users")
    old_hashes = dict(get_user_model().objects.values_list("username", "password"))
    demo_passwords(monkeypatch, "B8!synthetic-replacement")
    call_command("seed_users")
    assert dict(get_user_model().objects.values_list("username", "password")) == old_hashes
    call_command("seed_users", reset_passwords=True)
    assert all(
        user.check_password(os.environ[f"OUTAGE_{user.username.upper()}_PASSWORD"])
        for user in get_user_model().objects.all()
    )


@pytest.mark.django_db
def test_seed_refuses_an_existing_privileged_or_unclassified_name(monkeypatch):
    demo_passwords(monkeypatch)
    get_user_model().objects.create_user(username="viewer", password="existing-synthetic")
    with pytest.raises(CommandError, match="operator reconciliation"):
        call_command("seed_users", reset_passwords=True)
    assert Persona.objects.count() == 0


@pytest.mark.django_db
def test_production_seed_reads_file_passwords_and_never_reseeds_existing_users(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("OUTAGE_SECRET_FILES_ONLY", "1")
    passwords = {}
    for role in Persona.Role.values:
        setting = f"OUTAGE_{role.upper()}_PASSWORD"
        monkeypatch.delenv(setting, raising=False)
        passwords[role] = f"File-only-{role}-R8!synthetic-canary"
        path = tmp_path / f"{role}-password"
        path.write_text(passwords[role], encoding="utf-8")
        monkeypatch.setenv(f"{setting}_FILE", str(path))
    call_command("seed_users")
    before = dict(get_user_model().objects.values_list("username", "password"))
    assert all(
        user.check_password(passwords[user.username]) for user in get_user_model().objects.all()
    )
    for role in Persona.Role.values:
        monkeypatch.setenv(f"OUTAGE_{role.upper()}_PASSWORD_FILE", str(tmp_path / "missing"))
    call_command("seed_users")
    assert dict(get_user_model().objects.values_list("username", "password")) == before
    assert "canary" not in capsys.readouterr().out


@pytest.mark.django_db
def test_production_seed_rejects_a_missing_password_file_without_partial_accounts(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OUTAGE_SECRET_FILES_ONLY", "1")
    for role in Persona.Role.values:
        setting = f"OUTAGE_{role.upper()}_PASSWORD"
        monkeypatch.delenv(setting, raising=False)
        monkeypatch.setenv(f"{setting}_FILE", str(tmp_path / "missing-private-path-canary"))
    with pytest.raises(CommandError) as failure:
        call_command("seed_users")
    assert "canary" not in str(failure.value)
    assert get_user_model().objects.count() == 0


def test_settings_require_secret_and_production_mode_uses_secure_cookies(tmp_path):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"OUTAGE_DJANGO_SECRET_KEY", "PYTHONPATH", "PYTHONHOME"}
    }
    env["OUTAGE_STATE_DIR"] = str(tmp_path / "state")
    absent = subprocess.run(
        [sys.executable, "-I", "-c", "import outage_explorer.web.settings"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert absent.returncode != 0 and "OUTAGE_DJANGO_SECRET_KEY must be configured" in absent.stderr
    env.update(
        OUTAGE_DJANGO_SECRET_KEY="synthetic-test-secret-never-deploy-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        OUTAGE_LOCAL_HTTP="0",
    )
    configured = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import json; from outage_explorer.web import settings as s; print(json.dumps([s.DEBUG,s.SESSION_COOKIE_SECURE,s.CSRF_COOKIE_SECURE,s.SECURE_SSL_REDIRECT,s.ALLOWED_HOSTS]))",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert configured.returncode == 0, configured.stderr
    assert json.loads(configured.stdout) == [False, True, True, True, ["127.0.0.1", "localhost"]]


def test_production_settings_use_signing_key_file_without_populating_environment(tmp_path):
    secret = "synthetic-file-signing-key-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    path = tmp_path / "private-signing-path-canary"
    path.write_text(secret, encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "OUTAGE_DJANGO_SECRET_KEY"}
    }
    env.update(
        OUTAGE_SECRET_FILES_ONLY="1",
        OUTAGE_DJANGO_SECRET_KEY_FILE=str(path),
        OUTAGE_STATE_DIR=str(tmp_path / "state"),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import os; from outage_explorer.web import settings; "
            "assert len(settings.SECRET_KEY)>50; "
            "assert 'OUTAGE_DJANGO_SECRET_KEY' not in os.environ; print('configured')",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "configured"
    env["OUTAGE_DJANGO_SECRET_KEY"] = "synthetic-env-canary"
    rejected = subprocess.run(
        [sys.executable, "-I", "-c", "import outage_explorer.web.settings"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert rejected.returncode != 0 and "must be configured" in rejected.stderr
    assert "canary" not in rejected.stderr and secret not in rejected.stderr


@pytest.mark.parametrize(
    "weak_secret",
    [
        "too-short",
        "a" * 64,
        "django-insecure-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
    ],
)
def test_weak_deployment_secrets_fail_closed(tmp_path, weak_secret):
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(OUTAGE_DJANGO_SECRET_KEY=weak_secret, OUTAGE_STATE_DIR=str(tmp_path / "state"))
    result = subprocess.run(
        [sys.executable, "-I", "-c", "import outage_explorer.web.settings"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and "OUTAGE_DJANGO_SECRET_KEY must be configured" in result.stderr
    assert weak_secret not in result.stderr


def deployed_settings(tmp_path, **values):
    """Import deployed settings in an isolated process, without local configuration files."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "OUTAGE_ALLOWED_HOSTS", "OUTAGE_DEPLOY_REVISION"}
    }
    env.update(
        OUTAGE_DJANGO_SECRET_KEY="synthetic-deployment-key-only-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        OUTAGE_STATE_DIR=str(tmp_path / "state"),
        OUTAGE_LOCAL_HTTP="0",
        **values,
    )
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import json; from outage_explorer.web import settings as s; "
            "print(json.dumps([s.ALLOWED_HOSTS,s.OUTAGE_DEPLOY_REVISION]))",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("revision", ["a1" * 20, "b2" * 32])
def test_hosted_hosts_and_full_git_revision_are_explicit(tmp_path, revision):
    result = deployed_settings(
        tmp_path,
        OUTAGE_ALLOWED_HOSTS=" Arkham.example,127.0.0.1,[::1],arkham.example ",
        OUTAGE_DEPLOY_REVISION=revision,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [["arkham.example", "127.0.0.1", "[::1]"], revision]


@pytest.mark.parametrize(
    "hosts",
    [
        "",
        "*",
        ".example.com",
        "https://arkham.example",
        "arkham.example:443",
        "good.example,",
        "[bad]",
    ],
)
def test_hosted_hosts_reject_missing_or_unbounded_host_configuration(tmp_path, hosts):
    result = deployed_settings(tmp_path, OUTAGE_ALLOWED_HOSTS=hosts)
    assert result.returncode != 0
    assert "OUTAGE_ALLOWED_HOSTS must contain" in result.stderr


@pytest.mark.parametrize("revision", ["main", "a1" * 19, "z" * 40, "a" * 40 + "\nsecret"])
def test_deployment_revision_rejects_arbitrary_metadata_without_printing_it(tmp_path, revision):
    result = deployed_settings(tmp_path, OUTAGE_DEPLOY_REVISION=revision)
    assert result.returncode != 0
    assert "OUTAGE_DEPLOY_REVISION must be a full hexadecimal Git object ID" in result.stderr
    assert revision not in result.stderr
