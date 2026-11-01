"""Local setup creates real accounts without code defaults or credential output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from outage_explorer.web import local


def test_fresh_operator_init_is_private_repeatable_and_uses_real_password_hashes(tmp_path):
    project = Path(__file__).resolve().parents[1]
    state = tmp_path / "private-service"
    environment = dict(os.environ)
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    environment.pop("OUTAGE_DJANGO_SECRET_KEY", None)
    environment["OUTAGE_STATE_DIR"] = str(state)
    environment["OUTAGE_PROJECT_DIR"] = str(project)
    environment["OUTAGE_LOCAL_HTTP"] = "1"
    command = [sys.executable, "-m", "outage_explorer.web.local", "init"]
    first = subprocess.run(
        command, cwd=project, env=environment, capture_output=True, text=True, timeout=30
    )
    assert first.returncode == 0, first.stderr
    path = state / "local-config.json"
    original = path.read_bytes()
    values = json.loads(original)
    assert len(values["OUTAGE_DJANGO_SECRET_KEY"]) >= 50
    assert len(set(values.values())) == 4
    assert all(value not in first.stdout + first.stderr for value in values.values())
    script = """
import django,json,os
from pathlib import Path
config=json.loads(Path(os.environ['OUTAGE_STATE_DIR'],'local-config.json').read_text())
os.environ['OUTAGE_DJANGO_SECRET_KEY']=config['OUTAGE_DJANGO_SECRET_KEY']
os.environ['DJANGO_SETTINGS_MODULE']='outage_explorer.web.settings'
django.setup()
from django.contrib.auth import get_user_model
users=list(get_user_model().objects.select_related('persona').all())
assert len(users)==3
for u in users:
    assert u.persona.role==u.username and not u.is_staff and not u.is_superuser
    assert u.password.startswith('pbkdf2_sha256$')
    assert u.check_password(config['OUTAGE_'+u.username.upper()+'_PASSWORD'])
print(json.dumps({u.username:u.password for u in users}))
"""
    check = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 0, check.stderr
    original_hashes = json.loads(check.stdout)
    again = subprocess.run(
        command, cwd=project, env=environment, capture_output=True, text=True, timeout=30
    )
    assert again.returncode == 0, again.stderr
    assert path.read_bytes() == original
    verify = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert verify.returncode == 0 and json.loads(verify.stdout) == original_hashes
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_local_serve_drops_api_and_seed_credentials(monkeypatch, tmp_path):
    state = tmp_path / "service"
    local._configuration(state, create=True)
    monkeypatch.setenv("OUTAGE_STATE_DIR", str(state))
    monkeypatch.setenv("OUTAGE_DJANGO_SECRET_KEY", "synthetic-old-key")
    monkeypatch.setenv("EIA_API_KEY", "synthetic-upstream-key-do-not-inherit")
    for role in ("viewer", "analyst", "admin"):
        monkeypatch.setenv(f"OUTAGE_{role.upper()}_PASSWORD", "synthetic-seed-only")
    monkeypatch.setattr(sys, "argv", ["local", "serve", "--port", "8123"])
    from outage_explorer.web import serve

    observed = {}

    def inspect():
        observed.update(os.environ)
        assert sys.argv[-2:] == ["--port", "8123"]

    monkeypatch.setattr(serve, "main", inspect)
    local.main()
    assert "EIA_API_KEY" not in observed
    assert all(
        f"OUTAGE_{role.upper()}_PASSWORD" not in observed for role in ("viewer", "analyst", "admin")
    )
    assert len(observed["OUTAGE_DJANGO_SECRET_KEY"]) >= 50


def test_local_missing_configuration_fails_without_creating_credentials(tmp_path):
    environment = dict(os.environ)
    environment["OUTAGE_STATE_DIR"] = str(tmp_path / "not-initialized")
    result = subprocess.run(
        [sys.executable, "-m", "outage_explorer.web.local", "serve"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"] == "local_configuration_failed"
    assert not (Path(environment["OUTAGE_STATE_DIR"]) / "local-config.json").exists()
