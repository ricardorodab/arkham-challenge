"""Exercise production file secrets in disposable Linux containers without cloud access.

Use an already-built image, synthetic credentials and uniquely named data/state volumes.
The real production initializer runs twice, while app/worker mounts expose only their own
credential files. Existing containers are stopped and restarted after a ``current`` symlink
change to verify that a fresh volatile secret generation can be used after host startup.
Only static failure messages and acceptance booleans are printed; credentials stay in files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAMES = (
    "OUTAGE_DJANGO_SECRET_KEY",
    "EIA_API_KEY",
    "OUTAGE_VIEWER_PASSWORD",
    "OUTAGE_ANALYST_PASSWORD",
    "OUTAGE_ADMIN_PASSWORD",
)
FILES = {
    "app": {"django-secret-key"},
    "worker": {"django-secret-key", "eia-api-key"},
    "init": {"django-secret-key", "viewer-password", "analyst-password", "admin-password"},
}
PROBE = """
import hashlib,json,os
from pathlib import Path
from outage_explorer.runtime_secrets import SecretConfigurationError,read_secret
from outage_explorer.web import settings
assert os.getuid()==10001
names=('OUTAGE_DJANGO_SECRET_KEY','EIA_API_KEY','OUTAGE_VIEWER_PASSWORD','OUTAGE_ANALYST_PASSWORD','OUTAGE_ADMIN_PASSWORD')
assert all(name not in os.environ for name in names)
folder=Path('/run/secrets')
files={path.name:{'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'inode':path.stat().st_ino} for path in folder.iterdir()}
resolved={}
for name in names:
    try: value=read_secret(name,required=True)
    except SecretConfigurationError: continue
    resolved[name]=hashlib.sha256(value.encode()).hexdigest()
print(json.dumps({'uid':os.getuid(),'files':files,'resolved':resolved,'signing_key':hashlib.sha256(settings.SECRET_KEY.encode()).hexdigest()}))
"""


def require(condition, message):
    """Raise a fixed message rather than exposing Docker output or synthetic secrets."""
    if not condition:
        raise AssertionError(message)


class Acceptance:
    """Own only the temporary containers, volumes and fixture directory created here."""

    def __init__(self, docker, image, root):
        self.docker, self.image, self.root = docker, image, root
        self.project = "arkham-file-secrets-" + uuid.uuid4().hex[:12]
        self.containers = []
        self.volumes = []
        self.stage = "prerequisites"
        self.completed = []
        self.credentials = {
            "django-secret-key": secrets.token_urlsafe(48),
            "eia-api-key": "synthetic-" + secrets.token_hex(16),
            **{
                f"{role}-password": "Synthetic-" + secrets.token_urlsafe(24) + "-A9!"
                for role in ("viewer", "analyst", "admin")
            },
        }
        self.rotated = {
            **self.credentials,
            "eia-api-key": "synthetic-rotated-" + secrets.token_hex(16),
            **{
                f"{role}-password": "Rotated-" + secrets.token_urlsafe(24) + "-B8!"
                for role in ("viewer", "analyst", "admin")
            },
        }
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {*NAMES, *(name + "_FILE" for name in NAMES), "OUTAGE_SECRET_FILES_ONLY"}
        }

    def command(self, arguments, *, check=True, timeout=90):
        result = subprocess.run(
            [self.docker, *arguments],
            capture_output=True,
            text=True,
            env=self.environment,
            timeout=timeout,
            check=False,
        )
        if check:
            require(result.returncode == 0, f"Docker command failed during {self.stage}.")
        return result

    def prepare(self):
        require(os.name == "posix", "Production secret acceptance requires a Linux host.")
        require(
            self.command(["info", "--format", "{{.OSType}}"]).stdout.strip() == "linux",
            "Docker must run Linux containers.",
        )
        self.command(["image", "inspect", self.image])
        for generation, values in (
            ("generation-a", self.credentials),
            ("generation-b", self.rotated),
        ):
            for role, names in FILES.items():
                directory = self.root / generation / role
                directory.mkdir(parents=True)
                directory.chmod(0o755)
                for name in names:
                    path = directory / name
                    path.write_text(values[name], encoding="utf-8")
                    path.chmod(0o444)
        (self.root / "current").symlink_to("generation-a", target_is_directory=True)
        for suffix in ("data", "state"):
            volume = self.project + "-" + suffix
            self.command(
                ["volume", "create", "--label", f"arkham.acceptance={self.project}", volume]
            )
            self.volumes.append(volume)
        self.command(
            [
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--user",
                "0:0",
                "--read-only",
                *self.data_mounts(),
                self.image,
                "python",
                "-c",
                "import os;[(os.chown(p,10001,10001),os.chmod(p,0o700)) for p in ['/appdata/data','/appdata/state']]",
            ]
        )

    def data_mounts(self):
        return [
            argument
            for name in ("data", "state")
            for argument in (
                "--mount",
                f"type=volume,source={self.project}-{name},target=/appdata/{name}",
            )
        ]

    def start(self, suffix, role, command, *, detached=False):
        name = self.project + "-" + suffix
        self.containers.append(name)
        environment = {
            "OUTAGE_SECRET_FILES_ONLY": "1",
            "OUTAGE_DJANGO_SECRET_KEY_FILE": "/run/secrets/django-secret-key",
            "OUTAGE_LOCAL_HTTP": "0",
            "OUTAGE_ALLOWED_HOSTS": "localhost,127.0.0.1",
            "OUTAGE_DEPLOY_REVISION": "a" * 40,
            "OUTAGE_DATA_DIR": "/appdata/data",
            "OUTAGE_STATE_DIR": "/appdata/state",
        }
        if role == "worker":
            environment["EIA_API_KEY_FILE"] = "/run/secrets/eia-api-key"
        elif role == "init":
            environment.update(
                {
                    f"OUTAGE_{account.upper()}_PASSWORD_FILE": f"/run/secrets/{account}-password"
                    for account in ("viewer", "analyst", "admin")
                }
            )
        result = self.command(
            [
                "run",
                *(["--detach"] if detached else []),
                "--pull",
                "never",
                "--name",
                name,
                "--label",
                f"arkham.acceptance={self.project}",
                "--network",
                "none",
                "--user",
                "10001:10001",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "128",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
                *self.data_mounts(),
                "--mount",
                f"type=bind,source={ROOT / 'deploy' / 'app-control.py'},target=/deployment/app-control.py,readonly",
                "--mount",
                f"type=bind,source={self.root / 'current' / role},target=/run/secrets,readonly",
                *(
                    argument
                    for key, value in environment.items()
                    for argument in ("--env", f"{key}={value}")
                ),
                self.image,
                *command,
            ]
        )
        return name, result.stdout.strip()

    def inspect(self, container, role):
        result = json.loads(self.command(["inspect", container]).stdout)[0]
        environment = result["Config"]["Env"]
        keys = {entry.split("=", 1)[0] for entry in environment}
        require(
            not set(NAMES) & keys,
            "A production container received a secret-valued environment setting.",
        )
        serialized = json.dumps(environment)
        require(
            all(
                value not in serialized
                for value in {*self.credentials.values(), *self.rotated.values()}
            ),
            "Docker inspect exposed a credential value.",
        )
        require(
            result["Config"]["User"] == "10001:10001",
            "Production containers must run as UID 10001.",
        )
        mount = next(item for item in result["Mounts"] if item["Destination"] == "/run/secrets")
        require(not mount["RW"], "Role secret directories must be mounted read-only.")
        require(
            mount["Source"] == str(self.root / "current" / role),
            "Docker canonicalized the secret-generation alias; boot must recreate containers.",
        )

    def probe(self, container, role, values):
        self.inspect(container, role)
        result = json.loads(self.command(["exec", container, "python", "-c", PROBE]).stdout)
        require(
            result["uid"] == 10001 and set(result["files"]) == FILES[role],
            "A role can access another role's secret files.",
        )
        for name in FILES[role]:
            require(
                result["files"][name]["sha256"]
                == hashlib.sha256(values[name].encode()).hexdigest(),
                "A role loaded the wrong secret generation.",
            )
        expected_settings = {"OUTAGE_DJANGO_SECRET_KEY"}
        if role == "worker":
            expected_settings.add("EIA_API_KEY")
        elif role == "init":
            expected_settings.update(
                {"OUTAGE_VIEWER_PASSWORD", "OUTAGE_ANALYST_PASSWORD", "OUTAGE_ADMIN_PASSWORD"}
            )
        require(
            set(result["resolved"]) == expected_settings,
            "A credential resolved outside its role boundary.",
        )
        return result

    def wait_health(self, app):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            result = self.command(
                ["exec", app, "python", "/deployment/app-control.py", "health"],
                check=False,
                timeout=10,
            )
            if result.returncode == 0:
                require(
                    json.loads(result.stdout) == {"healthy": True, "revision": "a" * 40},
                    "File-secret application health returned the wrong revision.",
                )
                return
            time.sleep(0.4)
        raise AssertionError("The file-secret production app did not become healthy.")

    def state(self, app):
        return json.loads(
            self.command(["exec", app, "python", "/deployment/app-control.py", "inspect"]).stdout
        )

    def run(self):
        self.prepare()
        self.stage = "file-only production initialization"
        initializer, output = self.start(
            "initializer", "init", ["python", "/deployment/app-control.py", "init"]
        )
        require(
            json.loads(output.splitlines()[-1])["seeded"] is True,
            "Fresh file-secret initialization did not seed accounts.",
        )
        self.inspect(initializer, "init")
        probe, output = self.start("init-probe", "init", ["python", "-c", PROBE])
        require(
            set(json.loads(output)["files"]) == FILES["init"],
            "Bootstrap credentials were not isolated to initialization.",
        )
        self.inspect(probe, "init")
        self.completed.append(self.stage)

        self.stage = "nonroot app and worker role isolation"
        app, _ = self.start(
            "app",
            "app",
            ["python", "-m", "outage_explorer.web.serve", "--host", "0.0.0.0", "--port", "8000"],
            detached=True,
        )
        worker, _ = self.start(
            "worker",
            "worker",
            ["python", "-m", "outage_explorer.web.manage", "run_refresh_worker"],
            detached=True,
        )
        self.wait_health(app)
        prior_app = self.probe(app, "app", self.credentials)
        self.probe(worker, "worker", self.credentials)
        before = self.state(app)
        require(
            before["web"]["tables"]["auth_user"]["rows"] == 3,
            "Initialization did not create exactly three users.",
        )
        self.completed.append(self.stage)

        self.stage = "restart through a refreshed current secret generation"
        self.command(["stop", "--time", "15", app, worker])
        alias = self.root / "next"
        alias.symlink_to("generation-b", target_is_directory=True)
        alias.replace(self.root / "current")
        output = self.command(["start", "--attach", initializer]).stdout
        require(
            json.loads(output.splitlines()[-1])["seeded"] is False,
            "Repeated production initialization reset bootstrap accounts.",
        )
        self.command(["start", app, worker])
        self.wait_health(app)
        refreshed_app = self.probe(app, "app", self.rotated)
        self.probe(worker, "worker", self.rotated)
        require(
            prior_app["files"]["django-secret-key"]["inode"]
            != refreshed_app["files"]["django-secret-key"]["inode"],
            "Restarted app retained the old secret-directory inode.",
        )
        require(
            prior_app["signing_key"] == refreshed_app["signing_key"],
            "Refreshing the secret generation changed the Django signing key.",
        )
        require(
            self.state(app) == before,
            "Repeated initialization or restart changed users, password hashes or application state.",
        )
        self.completed.append(self.stage)

    def cleanup(self):
        """Remove only resources named and created by this acceptance instance."""
        self.stage = "scoped cleanup"
        cleaned = True
        for container in self.containers:
            result = self.command(["rm", "--force", container], check=False, timeout=30)
            cleaned &= result.returncode == 0 or "No such container" in result.stderr
        for volume in self.volumes:
            result = self.command(["volume", "rm", volume], check=False, timeout=30)
            cleaned &= result.returncode == 0 or "no such volume" in result.stderr.lower()
        require(cleaned, "Acceptance resources could not all be removed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", default=shutil.which("docker") or "docker")
    parser.add_argument("--image", default="arkham:ci")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="arkham-file-secrets-") as directory:
        acceptance = Acceptance(args.docker, args.image, Path(directory))
        passed, cleaned, failure, stage, message = False, False, None, None, None
        try:
            acceptance.run()
            passed = True
        except (AssertionError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            failure, stage = type(error).__name__, acceptance.stage
            message = str(error) if isinstance(error, AssertionError) else None
        finally:
            try:
                acceptance.cleanup()
                cleaned = True
            except (AssertionError, OSError, subprocess.SubprocessError):
                pass
        print(
            json.dumps(
                {
                    "passed": passed and cleaned,
                    "completed": acceptance.completed,
                    "failure_type": failure,
                    "failed_stage": stage,
                    "failure_message": message,
                    "cleanup_succeeded": cleaned,
                },
                sort_keys=True,
            )
        )
        return 0 if passed and cleaned else 1


if __name__ == "__main__":
    sys.exit(main())
