"""Exercise a built image through isolated Compose volumes and host HTTP, offline.

Only Python's standard library and the Docker CLI/Compose plugin are needed on the host.
The script never builds an image, uses an operator's state, or logs generated passwords.
It removes only the unique Compose project it created, including its disposable volumes.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent
EXPECTED_ASSETS = {
    "cjs": "3316baf528f80bf2564577d81ea107afc1031fa55f27c2e845ee04b47602ffbb",
    "wasm": "3abdec74989dcc54d2f2ea5621f611f3c45db1e7dff2f408476014d82beb2029",
    "extension": "4845705bbd69fc9ad52878d96a505c73cae4a6c509822079cc2413e5eb437f95",
}


def require(condition, message):
    """Raise a static assertion message that cannot expose HTTP bodies or credentials."""
    if not condition:
        raise AssertionError(message)


class Browser:
    """Keep independent cookie/CSRF state while calling the published host port."""

    def __init__(self, base):
        self.base = base
        self.csrf = None
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def request(self, path, payload=None, *, expected=200, method=None):
        headers = {"Origin": self.base}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers.update({"Content-Type": "application/json", "X-CSRFToken": self.csrf or ""})
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            response = self.opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            require(response.status == expected, "Unexpected HTTP status during acceptance.")
            body = response.read(3 * 1024 * 1024)
            content_type = response.headers.get("Content-Type", "")
        return json.loads(body) if "json" in content_type else body

    def login(self, username, password, *, role=None):
        self.csrf = self.request("/api/v1/auth/csrf")["csrf_token"]
        prior = self.csrf
        result = self.request("/api/v1/auth/login", {"username": username, "password": password})
        self.csrf = result["csrf_token"]
        require(bool(self.csrf) and self.csrf != prior, "Login must rotate the CSRF token.")
        require(
            result["user"]["persona"] == (role or username), "Login returned the wrong persona."
        )


class Acceptance:
    """Own bounded CLI invocations and resources of one uniquely named Compose project."""

    def __init__(self, docker, image):
        self.docker = docker
        self.image = image
        self.project = "arkham-acceptance-" + uuid.uuid4().hex[:12]
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("COMPOSE_")
            and key not in {"OUTAGE_PORT", "EIA_API_KEY", "OUTAGE_DJANGO_SECRET_KEY"}
            and not (key.startswith("OUTAGE_") and key.endswith("_PASSWORD"))
        }
        self.environment["OUTAGE_PORT"] = "0"
        self.environment["OUTAGE_IMAGE"] = image
        self.environment["EIA_API_KEY"] = ""
        docker_parent = str(Path(docker).resolve().parent)
        self.environment["PATH"] = docker_parent + os.pathsep + self.environment.get("PATH", "")
        mount = {
            "type": "volume",
            "source": "acceptance_fixture",
            "target": "/acceptance",
            "read_only": True,
        }
        self.override = json.dumps(
            {
                "services": {
                    name: {"image": image, **({"volumes": [mount]} if name != "init" else {})}
                    for name in ("app", "init", "worker")
                },
                "volumes": {"acceptance_fixture": {}},
            }
        )
        self.fixture_volume = self.project + "_acceptance_fixture"
        self.stage = "prerequisites"
        self.completed = []
        self.owns_project = False

    def command(self, arguments, *, compose=False, timeout=120):
        prefix = [self.docker]
        if compose:
            prefix += [
                "compose",
                "--project-name",
                self.project,
                "--project-directory",
                str(ROOT),
                "--env-file",
                os.devnull,
                "-f",
                str(ROOT / "compose.yaml"),
                "-f",
                "-",
            ]
        result = subprocess.run(
            [*prefix, *arguments],
            input=self.override if compose else None,
            capture_output=True,
            text=True,
            env=self.environment,
            timeout=timeout,
            check=False,
        )
        require(
            result.returncode == 0,
            f"Docker {arguments[0]} command failed during {self.stage}.",
        )
        return result.stdout.strip()

    def fixture(self, action, *, worker=False, failure=False):
        arguments = ["run", "--rm", "--no-deps", "worker"] if worker else ["exec", "-T", "app"]
        arguments += ["python", "/acceptance/fixture.py", action]
        if failure:
            arguments.append("--failure")
        output = self.command(arguments, compose=True)
        return json.loads(output) if not worker else None

    def state(self):
        return self.fixture("state")

    def observe_worker(self, browser, job, *, failure=False):
        """Poll real persisted progress over HTTP during an offline separate worker."""
        observed = []
        deadline = time.monotonic() + 90
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(self.fixture, "worker", worker=True, failure=failure)
            while not worker.done():
                current = browser.request(job["status_url"])
                observed.append(current)
                require(time.monotonic() < deadline, "Synthetic progress observation timed out.")
                time.sleep(0.1)
            worker.result()
        current = browser.request(job["status_url"])
        observed.append(current)
        require(
            any(item["status"] == "running" and item["progress"] for item in observed),
            "No persisted running progress was visible over HTTP.",
        )
        if not failure:
            stages = {item["progress"].get("stage") for item in observed}
            require(
                {"extracting", "model_fleet", "exporting_model", "publishing"} <= stages,
                "Extraction, long model phases, export and publication must report real stages.",
            )
            require(
                any(item["progress"].get("total") == 10 for item in observed),
                "Fixed model-export progress was not observable.",
            )
        require(
            all(
                item["progress"].get("stage") == "exporting_model"
                or "total" not in item["progress"]
                for item in observed
            ),
            "Unknown source totals must not become an overall percentage.",
        )
        return current

    def prepare_fixture(self):
        """Copy the public fixture through Docker's API, without a host bind mount."""
        self.stage = "isolated read-only fixture volume"
        self.command(
            [
                "volume",
                "create",
                "--label",
                f"com.docker.compose.project={self.project}",
                "--label",
                "com.docker.compose.volume=acceptance_fixture",
                self.fixture_volume,
            ]
        )
        container = self.command(
            [
                "create",
                "--name",
                self.project + "-fixture-loader",
                "--label",
                f"com.docker.compose.project={self.project}",
                "--label",
                "com.docker.compose.service=acceptance_fixture_loader",
                "--mount",
                f"type=volume,source={self.fixture_volume},target=/acceptance",
                self.image,
            ]
        )
        try:
            self.command(
                ["cp", str(FIXTURES / "fixture.py"), container + ":/acceptance/fixture.py"]
            )
        finally:
            self.command(["rm", container])

    def wait_http(self, browser):
        deadline = time.monotonic() + 60
        while True:
            try:
                require(
                    browser.request("/api/v1/health/live") == {"status": "ok"},
                    "Invalid liveness response.",
                )
                return
            except (OSError, AssertionError, urllib.error.URLError):
                if time.monotonic() >= deadline:
                    raise AssertionError("Published HTTP service did not become live.") from None
                time.sleep(0.25)

    @staticmethod
    def wait_terminal_job(browser, status_url):
        """Allow normal queue polling while bounding a missing or stalled consumer."""
        deadline = time.monotonic() + 30
        while True:
            job = browser.request(status_url)
            if job["status"] not in {"queued", "running"}:
                return job
            require(time.monotonic() < deadline, "Automatic worker did not finish the queued job.")
            time.sleep(0.25)

    def check_image(self):
        self.stage = "image metadata and privacy"
        config = json.loads(
            self.command(["image", "inspect", self.image, "--format", "{{json .Config}}"])
        )
        require(
            config.get("User", "").split(":")[0] not in {"", "0", "root"},
            "Image must configure a nonroot user.",
        )
        secret_names = {
            "EIA_API_KEY",
            "OUTAGE_DJANGO_SECRET_KEY",
            *(f"OUTAGE_{role}_PASSWORD" for role in ("VIEWER", "ANALYST", "ADMIN")),
        }
        require(
            not any(item.split("=", 1)[0] in secret_names for item in config.get("Env", [])),
            "Image environment contains a credential input.",
        )
        output = self.command(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python",
                "--mount",
                f"type=volume,source={self.fixture_volume},target=/acceptance,readonly",
                self.image,
                "/acceptance/fixture.py",
                "image",
            ]
        )
        metadata = json.loads(output)
        require(metadata["uid"] != 0, "Image probe ran as root.")
        require(
            re.fullmatch(r"3\.(12|13|14)\.\d+", metadata["python"]), "Unsupported Python runtime."
        )
        require(
            re.fullmatch(r"v24\.(1[3-9]|[2-9]\d)\.\d+", metadata["node"]),
            "Unsupported Node runtime.",
        )
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        require(
            metadata["npm_version"]
            == lock["packages"]["node_modules/@duckdb/duckdb-wasm"]["version"],
            "Image npm package differs from the lockfile.",
        )
        for line in (ROOT / "requirements.txt").read_text().splitlines():
            if line and not line.startswith("#"):
                name, version = line.split("==")
                require(
                    metadata["packages"][name] == version,
                    "Image Python package differs from its runtime pin.",
                )
        expected_assets = {
            **EXPECTED_ASSETS,
            "worker": hashlib.sha256(
                (ROOT / "src/outage_explorer/sandbox/worker.cjs").read_bytes()
            ).hexdigest(),
        }
        require(metadata["asset_sha256"] == expected_assets, "Installed query asset hashes differ.")
        require(metadata["runtime"]["available"] is True, "Image query runtime is unavailable.")
        require(
            metadata["prohibited_files_present"] == [], "Image contains excluded local-state paths."
        )
        self.completed.append(self.stage)

    @staticmethod
    def check_publication(state):
        require(state["uid"] != 0, "Stateful command ran as root.")
        require(state["snapshot_id"] and state["model_id"], "Synthetic publication is missing.")
        require(
            state["snapshot_id"] == state["model_source_id"],
            "Source/model publications do not match.",
        )

    def check_cli_defaults(self, state):
        """Require CLI defaults to resolve the same mounted data as the Django service."""
        raw = json.loads(
            self.command(
                ["exec", "-T", "app", "python", "-m", "outage_explorer", "status"],
                compose=True,
            )
        )
        model = json.loads(
            self.command(
                ["exec", "-T", "app", "python", "-m", "outage_explorer", "model", "status"],
                compose=True,
            )
        )
        require(
            raw["current_snapshot"] == state["snapshot_id"]
            and model["current_model"] == state["model_id"]
            and model["manifest"]["source_snapshot_id"] == state["snapshot_id"],
            "Default CLI data directory differs from the service's persisted publication.",
        )

    def query(self, browser, model_id, expected_percent):
        result = browser.request(
            "/api/v1/query",
            {
                "dataset_id": "generator_daily",
                "model_id": model_id,
                "sql": "SELECT period, facility_id, generator_id, capacity_mw, outage_mw FROM generator_daily ORDER BY period, facility_id, generator_id LIMIT 1",
            },
        )
        require(
            result["rows"] == [["2026-09-01", "001", "01", "100.250000000", "10.025000000"]],
            "Real WASM query lost date, decimal or textual-ID precision.",
        )
        require(
            result["model_id"] == model_id and not result["cache_hit"],
            "Query did not execute against the selected new publication.",
        )
        fleet = browser.request(
            "/api/v1/query",
            {
                "dataset_id": "fleet_daily",
                "model_id": model_id,
                "sql": "SELECT period, offline_pct FROM fleet_daily ORDER BY period",
            },
        )
        require(
            [row[0] for row in fleet["rows"]] == ["2026-09-01", "2026-09-02", "2026-09-03"],
            "Synthetic query returned incorrect dates.",
        )
        require(
            Decimal(fleet["rows"][-1][1]) == Decimal(expected_percent),
            "Synthetic refresh value is incorrect.",
        )
        plan = {
            "model_id": model_id,
            "facility_id": "001",
            "sort": "outage_mw",
            "direction": "desc",
            "limit": 2,
            "filters": [{"column": "outage_mw", "op": "not_null"}],
        }
        page = browser.request("/api/v1/data/generator_daily", plan)
        require(
            page["returned_rows"] == 2
            and page["next_cursor"]
            and page["query"]["order"][0] == {"column": "outage_mw", "direction": "desc"},
            "Structured native browse did not apply ordering and a bounded seek page.",
        )
        names = [column["name"] for column in page["columns"]]
        require(
            all(row[names.index("facility_id")] == "001" for row in page["rows"]),
            "Structured native browse did not apply exact ID filtering.",
        )
        following = browser.request(
            "/api/v1/data/generator_daily", {**plan, "cursor": page["next_cursor"]}
        )

        def keys(rows):
            return {(row[0], row[1], row[2]) for row in rows}

        require(
            keys(page["rows"]).isdisjoint(keys(following["rows"])),
            "Structured seek repeated prior observations.",
        )
        literal = browser.request(
            "/api/v1/data/generator_daily", {"search": "%_", "model_id": model_id}
        )
        require(literal["rows"] == [], "Structured search interpreted literal wildcard text.")

    def check_accounts(self, clients):
        """Exercise account lifecycle and reinitialization in this empty synthetic workspace."""
        self.stage = "Admin account lifecycle and initialization persistence"
        admin = clients["admin"]
        clients["viewer"].request("/api/v1/users", expected=403)
        created = admin.request(
            "/api/v1/users",
            {"username": "docker.account", "role": "viewer", "first_name": "Container"},
            expected=201,
        )
        require(
            re.fullmatch(r"[A-Za-z0-9_-]{32}", created["generated_password"]),
            "Account creation must return a generated password exactly once.",
        )
        account = created["user"]
        reader = Browser(admin.base)
        reader.login(account["username"], created["generated_password"], role="viewer")
        endpoint = f"/api/v1/users/{account['id']}"
        changed = admin.request(
            endpoint,
            {
                "version": account["version"],
                "username": "docker.account.renamed",
                "role": "analyst",
            },
            method="PUT",
        )["user"]
        reader.request("/api/v1/auth/session", expected=401)
        directory = admin.request("/api/v1/users?search=docker.account")
        require(
            len(directory["users"]) == 1
            and directory["users"][0]["username"] == "docker.account.renamed"
            and "generated_password" not in directory,
            "The user directory must reflect the edit without returning passwords.",
        )
        # Normal Compose initialization must not recreate or overwrite managed identities.
        self.command(["run", "--rm", "--no-deps", "init"], compose=True)
        require(
            admin.request("/api/v1/users?search=docker.account")["users"] == directory["users"],
            "Repeated initialization changed a managed account.",
        )
        reset = admin.request(endpoint + "/password", {"version": changed["version"]})
        require(
            reset["generated_password"] != created["generated_password"]
            and not reset["reauthenticate"],
            "Password reset must replace the password without ending the acting Admin session.",
        )
        reader.login(changed["username"], reset["generated_password"], role="analyst")
        admin.request(endpoint, {"version": reset["user"]["version"]}, method="DELETE")
        reader.request("/api/v1/auth/session", expected=401)
        require(
            admin.request("/api/v1/users?search=docker.account")["users"] == [],
            "Deleted accounts must disappear from the directory.",
        )
        self.completed.append(self.stage)

    def run(self):
        self.command(["compose", "version"])
        existing_containers = self.command(
            [
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--format",
                "{{.ID}}",
            ]
        )
        existing_volumes = self.command(
            [
                "volume",
                "ls",
                "--filter",
                f"name={self.project}",
                "--format",
                "{{.Name}}",
            ]
        )
        require(
            not existing_containers and not existing_volumes,
            "Acceptance project name is already in use.",
        )
        self.owns_project = True
        self.prepare_fixture()
        self.check_image()
        self.stage = "fresh isolated initialization"
        # Exercise the documented dependency: init must finish before app starts.
        self.command(["up", "-d", "app"], compose=True)
        mapping = self.command(["port", "app", "8000"], compose=True)
        require(
            re.fullmatch(r"127\.0\.0\.1:\d+", mapping), "Host port must be published on loopback."
        )
        base = "http://" + mapping
        anonymous = Browser(base)
        self.wait_http(anonymous)
        require(b"Outage" in anonymous.request("/login/"), "Packaged login page is unavailable.")
        require(bool(anonymous.request("/assets/app.js")), "Packaged browser asset is unavailable.")
        require(
            anonymous.request("/api/v1/catalog", expected=401)["code"] == "authentication_required",
            "Anonymous catalog access was not denied.",
        )
        before = self.state()
        require(
            before["snapshot_id"] is None and before["model_id"] is None,
            "Fresh image unexpectedly contains analytical data.",
        )
        self.command(["run", "--rm", "--no-deps", "init"], compose=True)
        require(self.state() == before, "Repeated initialization changed existing credentials.")
        credentials = self.fixture("credentials")
        clients = {role: Browser(base) for role in credentials}
        for role, client in clients.items():
            client.login(role, credentials[role])
        require(
            clients["admin"].request("/api/v1/health/ready", expected=503)["status"] == 503,
            "Empty data must not report analytical readiness.",
        )
        self.completed.append(self.stage)

        self.check_accounts(clients)

        self.stage = "automatic worker reports missing configuration"
        worker_id = self.command(["ps", "-q", "worker"], compose=True)
        require(
            re.fullmatch(r"[0-9a-f]{64}", worker_id),
            "Standard app startup did not create a worker.",
        )
        worker_state = json.loads(
            self.command(["inspect", worker_id, "--format", "{{json .State}} "])
        )
        require(worker_state["Running"], "Standard app startup did not start the worker.")
        admin = clients["admin"]
        unconfigured_job = admin.request("/api/v1/refresh", {"mode": "recent"}, expected=202)
        unconfigured = self.wait_terminal_job(admin, unconfigured_job["status_url"])
        require(
            unconfigured["status"] == "failed"
            and unconfigured["error_code"] == "refresh_not_configured"
            and unconfigured["message"]
            == (
                "Refresh is not configured. Set EIA_API_KEY for the refresh worker, "
                "recreate the worker, and queue a new refresh."
            )
            and unconfigured["result"] == {}
            and unconfigured["started_at"] is not None
            and unconfigured["finished_at"] is not None,
            "Missing EIA key did not produce an actionable terminal job failure.",
        )
        worker_state = json.loads(
            self.command(["inspect", worker_id, "--format", "{{json .State}} "])
        )
        require(worker_state["Running"], "Missing EIA key stopped the continuous worker.")
        require(
            anonymous.request("/api/v1/health/live") == {"status": "ok"},
            "Missing EIA key stopped the app.",
        )
        unconfigured_state = self.state()
        require(
            unconfigured_state["snapshot_id"] is None
            and unconfigured_state["model_id"] is None
            and unconfigured_state["configuration_sha256"] == before["configuration_sha256"]
            and unconfigured_state["accounts_sha256"] == before["accounts_sha256"],
            "Missing-key failure changed data or account configuration.",
        )
        # Keep the existing upstream-injection probes deterministic with one consumer.
        self.command(["stop", "--timeout", "15", "worker"], compose=True, timeout=35)
        self.completed.append(self.stage)

        self.stage = "synthetic ingestion and authenticated real WASM"
        published = self.fixture("seed")
        self.check_publication(published)
        self.check_cli_defaults(published)
        viewer = clients["viewer"].request("/api/v1/catalog")
        require(
            {row["id"] for row in viewer["datasets"]} == {"national_daily", "fleet_daily"},
            "Viewer catalog exposes detail datasets.",
        )
        require(
            clients["viewer"].request("/api/v1/data/generator_daily", expected=403)["code"]
            == "dataset_forbidden",
            "Viewer detail authorization is missing.",
        )
        require(
            len(clients["analyst"].request("/api/v1/catalog")["datasets"]) == 10,
            "Analyst catalog is incomplete.",
        )
        ready = clients["admin"].request("/api/v1/health/ready")
        require(
            ready["query_runtime"]["available"] is True,
            "Admin readiness cannot resolve the runtime.",
        )
        self.query(clients["analyst"], published["model_id"], "20")
        self.completed.append(self.stage)

        self.stage = "separate successful and failing refresh workers"
        admin = clients["admin"]
        job = admin.request("/api/v1/refresh", {"mode": "recent"}, expected=202)
        require(
            admin.request(job["status_url"])["status"] == "queued",
            "HTTP enqueue unexpectedly consumed a refresh job.",
        )
        succeeded = self.observe_worker(admin, job)
        require(
            succeeded["status"] == "succeeded", "Synthetic worker did not complete the queued job."
        )
        require(
            succeeded["progress"]["stage"] == "completed"
            and succeeded["progress"]["rows_received"] == 21
            and succeeded["progress"]["pages_fetched"] == 3,
            "The completed job lost its actual pipeline counters.",
        )
        refreshed = self.state()
        self.check_publication(refreshed)
        require(
            refreshed["model_id"] != published["model_id"], "Refresh did not publish a new model."
        )
        require(
            succeeded["result"]["model_id"] == refreshed["model_id"]
            and succeeded["result"]["snapshot_id"] == refreshed["snapshot_id"],
            "Job receipt does not identify its publication.",
        )
        self.query(clients["analyst"], refreshed["model_id"], "30")
        failed_job = admin.request("/api/v1/refresh", {"mode": "recent"}, expected=202)
        failed = self.observe_worker(admin, failed_job, failure=True)
        require(
            failed["status"] == "failed" and failed["error_code"] == "upstream_refresh_failed",
            "Synthetic upstream failure was not recorded safely.",
        )
        retained = self.state()
        require(
            (retained["snapshot_id"], retained["model_id"])
            == (refreshed["snapshot_id"], refreshed["model_id"]),
            "Failed refresh changed the current publication.",
        )
        self.completed.append(self.stage)

        self.stage = "bounded stop and persistent restart/recreation"
        old_id = self.command(["ps", "-q", "app"], compose=True)
        self.command(["stop", "--timeout", "15", "app"], compose=True, timeout=35)
        stopped = json.loads(self.command(["inspect", old_id, "--format", "{{json .State}} "]))
        require(
            not stopped["Running"] and not stopped["OOMKilled"] and stopped["ExitCode"] in {0, 143},
            "SIGTERM required a forced kill or failed container exit.",
        )
        # Start the same stopped container without re-evaluating Compose init dependencies.
        self.command(["start", old_id])
        restarted_mapping = self.command(["port", "app", "8000"], compose=True)
        require(
            re.fullmatch(r"127\.0\.0\.1:\d+", restarted_mapping),
            "Restarted port must remain loopback-only.",
        )
        anonymous.base = "http://" + restarted_mapping
        admin.base = anonymous.base
        self.wait_http(anonymous)
        require(
            self.state() == retained, "Restart changed credentials, publications or job history."
        )
        require(
            admin.request("/api/v1/auth/session")["user"]["persona"] == "admin",
            "Persisted session did not survive restart.",
        )
        self.command(["up", "-d", "--no-deps", "--force-recreate", "app"], compose=True)
        new_id = self.command(["ps", "-q", "app"], compose=True)
        require(new_id != old_id, "Container was not replaced for persistence acceptance.")
        mapping = self.command(["port", "app", "8000"], compose=True)
        require(
            re.fullmatch(r"127\.0\.0\.1:\d+", mapping), "Recreated port must remain loopback-only."
        )
        restarted = Browser("http://" + mapping)
        self.wait_http(restarted)
        require(
            self.state() == retained, "Recreation changed credentials, publications or job history."
        )
        self.check_cli_defaults(retained)
        restarted.login("analyst", credentials["analyst"])
        # A distinct supported query guarantees execution instead of a retained result cache.
        result = restarted.request(
            "/api/v1/query",
            {
                "dataset_id": "generator_daily",
                "sql": "SELECT COUNT(*) AS observations FROM generator_daily",
            },
        )
        require(
            result["rows"] == [["12"]] and not result["cache_hit"],
            "Recreated service cannot execute its persisted model.",
        )
        self.completed.append(self.stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", default=shutil.which("docker") or "docker")
    parser.add_argument("--image", default="arkham:local")
    args = parser.parse_args()
    acceptance = Acceptance(args.docker, args.image)
    success = False
    failure = None
    failed_stage = None
    failure_message = None
    try:
        acceptance.run()
        success = True
    except (AssertionError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        failure = type(error).__name__
        failed_stage = acceptance.stage
        failure_message = str(error) if isinstance(error, AssertionError) else None
    finally:
        acceptance.stage = "scoped cleanup"
        try:
            if acceptance.owns_project:
                acceptance.command(
                    ["down", "--volumes", "--remove-orphans", "--timeout", "15"],
                    compose=True,
                    timeout=60,
                )
            cleaned = True
        except (AssertionError, OSError, subprocess.SubprocessError):
            cleaned = False
    print(
        json.dumps(
            {
                "passed": success and cleaned,
                "completed": acceptance.completed,
                "failure_type": failure,
                "failed_stage": failed_stage,
                "failure_message": failure_message,
                "cleanup_succeeded": cleaned,
                "project": acceptance.project,
            },
            sort_keys=True,
        )
    )
    return 0 if success and cleaned else 1


if __name__ == "__main__":
    sys.exit(main())
