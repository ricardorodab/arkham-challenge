"""Local setup creates real accounts without code defaults or credential output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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
    assert json.loads(first.stdout.splitlines()[-1])["seeded"] is True
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
    assert json.loads(again.stdout.splitlines()[-1])["seeded"] is False
    assert json.loads(again.stdout.splitlines()[-1])["users"] == []
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


def test_repeat_init_preserves_admin_managed_accounts_and_explicit_reset_is_opt_in(tmp_path):
    """Reinitialization must not recreate deleted/renamed defaults or restore old passwords."""
    project = Path(__file__).resolve().parents[1]
    state = tmp_path / "managed-service"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DJANGO_SETTINGS_MODULE", "OUTAGE_DJANGO_SECRET_KEY", "EIA_API_KEY"}
    }
    environment.update(
        OUTAGE_STATE_DIR=str(state),
        OUTAGE_DATA_DIR=str(tmp_path / "no-analytical-data"),
        OUTAGE_PROJECT_DIR=str(project),
        OUTAGE_LOCAL_HTTP="1",
    )
    command = [sys.executable, "-m", "outage_explorer.web.local", "init"]

    def child(arguments):
        result = subprocess.run(
            arguments, cwd=project, env=environment, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        return result

    first = child(command)
    assert json.loads(first.stdout.splitlines()[-1])["seeded"] is True
    configuration = (state / "local-config.json").read_bytes()
    bootstrap = """
import django,json,os
from pathlib import Path
config=json.loads(Path(os.environ['OUTAGE_STATE_DIR'],'local-config.json').read_text())
os.environ['OUTAGE_DJANGO_SECRET_KEY']=config['OUTAGE_DJANGO_SECRET_KEY']
os.environ['DJANGO_SETTINGS_MODULE']='outage_explorer.web.settings'
django.setup()
from django.contrib.auth import get_user_model
from outage_explorer.service.models import Persona
def snapshot():
    return {'users':list(get_user_model().objects.order_by('id').values()),
            'personas':list(Persona.objects.order_by('id').values())}
"""
    mutation = """
from django.test import Client
client=Client(enforce_csrf_checks=True,HTTP_HOST='127.0.0.1')
token=client.get('/api/v1/auth/csrf').json()['csrf_token']
result=client.post('/api/v1/auth/login', json.dumps({'username':'admin','password':config['OUTAGE_ADMIN_PASSWORD']}),content_type='application/json',HTTP_X_CSRFTOKEN=token)
assert result.status_code==200
client.defaults['HTTP_X_CSRFTOKEN']=result.json()['csrf_token']
def send(method,identifier,payload,suffix=''):
    response=getattr(client,method)('/api/v1/users/'+str(identifier)+suffix,json.dumps(payload),content_type='application/json')
    assert response.status_code==200
    return response.json()
users={row['username']:row for row in client.get('/api/v1/users').json()['users']}
target=users['analyst']
target=send('put',target['id'],{'version':target['version'],'username':'team.analysis','role':'viewer','is_active':False,'first_name':'Grace','last_name':'Hopper'})['user']
send('post',target['id'],{'version':target['version']},'/password')
viewer=users['viewer']
send('delete',viewer['id'],{'version':viewer['version']})
admin=users['admin']
admin=send('put',admin['id'],{'version':admin['version'],'username':'team.admin'})['user']
send('post',admin['id'],{'version':admin['version']},'/password')
assert not get_user_model().objects.filter(username__in=['viewer','analyst','admin']).exists()
print(json.dumps(snapshot(),default=str))
"""
    modified = json.loads(
        child([sys.executable, "-c", bootstrap + mutation]).stdout.splitlines()[-1]
    )
    repeated = child(command)
    report = json.loads(repeated.stdout.splitlines()[-1])
    assert report["initialized"] and report["seeded"] is False and report["users"] == []
    observed = child(
        [sys.executable, "-c", bootstrap + "\nprint(json.dumps(snapshot(),default=str))"]
    )
    assert json.loads(observed.stdout.splitlines()[-1]) == modified
    assert (state / "local-config.json").read_bytes() == configuration
    explicit = child(command + ["--reset-passwords"])
    assert json.loads(explicit.stdout.splitlines()[-1])["seeded"] is True
    verify = """
for role in ('viewer','analyst','admin'):
    user=get_user_model().objects.get(username=role)
    assert user.check_password(config['OUTAGE_'+role.upper()+'_PASSWORD'])
assert get_user_model().objects.filter(username__in=['team.admin','team.analysis']).count()==2
"""
    child([sys.executable, "-c", bootstrap + verify])


@pytest.mark.parametrize(
    "bind_arguments,expected_host",
    [([], "127.0.0.1"), (["--host", "0.0.0.0"], "0.0.0.0")],
)
def test_local_serve_drops_api_and_seed_credentials(
    monkeypatch, tmp_path, bind_arguments, expected_host
):
    state = tmp_path / "service"
    local._configuration(state, create=True)
    monkeypatch.setenv("OUTAGE_STATE_DIR", str(state))
    monkeypatch.setenv("OUTAGE_DJANGO_SECRET_KEY", "synthetic-old-key")
    monkeypatch.setenv("EIA_API_KEY", "synthetic-upstream-key-do-not-inherit")
    for role in ("viewer", "analyst", "admin"):
        monkeypatch.setenv(f"OUTAGE_{role.upper()}_PASSWORD", "synthetic-seed-only")
    monkeypatch.setattr(sys, "argv", ["local", "serve", *bind_arguments, "--port", "8123"])
    from outage_explorer.web import serve

    observed = {}

    def inspect():
        observed.update(os.environ)
        assert sys.argv[1:3] == ["--host", expected_host]
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
