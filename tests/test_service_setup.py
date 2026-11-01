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
