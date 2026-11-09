# Azure VM deployment

The production stack is separate from `compose.yaml`. It runs a fixed image digest with
independent web and refresh-worker processes, and Caddy terminates public HTTPS. The app
port is published only on loopback. Only Caddy's static private address is trusted for
forwarding the HTTPS scheme and client address. Inbound Azure NSG rules need TCP 80 and 443;
public SSH is not required because deployments use Azure Managed Run Command.

The host keeps `/srv/arkham/data` and `/srv/arkham/state` across every release, including
SQLite databases, committed WAL, users, password hashes, sessions, saved views, refresh
jobs, publication history and immutable analytical files. These are bind mounts independent
of the source checkout and image. No deployment removes volumes, resets passwords, or
downloads data. An empty installation has accounts and a healthy login page before a
publication exists. Live ingestion needs an operator-provided EIA key.

## GitHub Actions setup and use

The [CI workflow](../.github/workflows/ci.yml) runs the Python, JavaScript, browser and
container checks before deployment. For a push to `main`, it publishes the already accepted
image to public GHCR without rebuilding, records its immutable digest, signs in to Azure
using OIDC, and invokes Azure Managed Run Command on the VM. Deployment succeeds only after
certificate-validated public HTTPS returns the exact commit in `X-Outage-Revision`.
Pull requests and other branches run checks without publishing or deploying.

Set these six nonsecret repository **Actions variables**:

| Variable | Value |
| --- | --- |
| `AZURE_CLIENT_ID` | Application/client ID of the Azure deployment identity |
| `AZURE_TENANT_ID` | Azure tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Subscription containing the VM |
| `AZURE_RESOURCE_GROUP` | VM resource group |
| `AZURE_VM_NAME` | VM name |
| `OUTAGE_PUBLIC_HOST` | Exact public DNS hostname resolving to the VM |

Configure the identity's federated credential with issuer
`https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`, and
the repository's exact main-branch subject. This repository uses
`repo:ricardorodab@9595097/arkham-challenge@1448062072:ref:refs/heads/main`.
The numeric owner/repository IDs are part of its immutable subject; do not substitute the
older name-only format. Check the actual repository OIDC configuration when copying this
setup. See [GitHub's OIDC reference](https://docs.github.com/en/actions/reference/security/oidc).

Assign the deployment identity a custom role **at the single VM resource scope** with:

```text
Microsoft.Compute/virtualMachines/read
Microsoft.Compute/virtualMachines/runCommands/read
Microsoft.Compute/virtualMachines/runCommands/write
Microsoft.Compute/virtualMachines/runCommands/delete
```

The plural `runCommands` permissions apply to managed command resources. The helper uses
VM read plus managed-command read/write/delete and automatically deletes completed command
records. It retains any command whose terminal state is unknown, allowing an in-progress
upgrade to finish. [Azure's Compute permissions](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/compute)
lists these actions. No open SSH port, Azure client secret, stored registry PAT or GitHub
credential secret is needed. The workflow's short-lived `GITHUB_TOKEN` publishes the public
code image. The VM pulls its exact digest anonymously; no GitHub or registry credential
is sent through Run Command or retained on the VM.

Open a pull request, pass the required CI and security checks, and merge into protected
`main`. Administrators follow the same rules; independent approvals can be required after
another trusted reviewer joins. Follow the resulting main-branch CI run's deployment job.
Upgrades are serialized. If deployment
is deferred because a refresh is queued/running, let that work complete and use **Re-run
failed jobs** in the same Actions run; it retries the accepted digest. Source releases and
persistent application data are separate. Store the EIA key in the production vault before
using refresh. Deployment does not import local
accounts/data, reset existing accounts, or implicitly ingest analytical data.

## Host setup

Run `bootstrap-vm.sh <public-DNS-hostname>` as root on Ubuntu 24.04. The script installs
Docker and Compose if absent, enables Docker, and creates private storage paths plus
nonsecret JSON metadata. Production secrets must already exist in Key Vault. The hostname
must resolve to the VM's static public IP before
Caddy can obtain a trusted certificate. An Azure public-IP DNS label can supply this
hostname without purchasing a domain.

### Production secrets

The production source of truth is `arkhamkv`, reached over its approved private endpoint
and private DNS. Public vault access stays disabled. The VM's **system-assigned runtime
identity** has `Key Vault Secrets User` at this application vault. GitHub's deployment
identity remains separate and has no direct vault data-plane role. It can execute root
deployment commands, so trusted deployment code remains part of the secret-access boundary.

| Vault secret | Consumer | Purpose |
| --- | --- | --- |
| `django-secret-key` | App, worker, initializer | Stable Django signatures and session validation |
| `eia-api-key` | Refresh worker | Live EIA extraction; absent means refresh is not configured |
| `bootstrap-viewer-password` | Initializer on first setup | Initial Viewer account only |
| `bootstrap-analyst-password` | Initializer on first setup | Initial Analyst account only |
| `bootstrap-admin-password` | Initializer on first setup | Initial Admin account only |

Bootstrap secrets are not a record of current user passwords. Django stores hashes in the
application database; account edits and resets do not update these initial values. Existing
accounts are never reseeded during upgrades. Preserve the Django key during migration;
changing it invalidates signatures and requires an explicit rotation plan.

`key-vault-secrets.py fetch` runs only on the host, authenticates through managed identity,
and atomically publishes complete, role-specific directories on `/run` **tmpfs**. Containers
receive their own read-only directory at `/run/secrets`; they cannot access the VM metadata
endpoint or obtain its identity token. Files are owned by UID10001 with mode `0400`, beneath
role directories with mode `0500` and a root-only generation parent. The `_FILE` settings
contain paths, never credential values. `OUTAGE_SECRET_FILES_ONLY=1` forbids secret-value
environment variables. No production secret `.env` files are loaded or generated.
Production commands use `--env-file /dev/null` to disable Compose's automatic `.env`
discovery; the remaining environment settings are nonsecret paths and deployment options.

Root-owned `/etc/arkham/key-vault.json` stores only the fixed vault URL, bootstrap policy,
and a SHA-256 pin of the existing high-entropy Django key. `/etc/arkham/deployment.json`
stores the public hostname; each release's `release.json` stores its image digest. These
are nonsecret metadata, not alternative credential stores.

Docker's systemd pre-start hook restores the tmpfs files before containers restart after a
reboot. A cold start requires working private DNS, network connectivity, Key Vault and
runtime identity access; there is no persistent plaintext fallback. A failed secret fetch
during deployment happens before pausing the existing app. Retrieved values and access
tokens never enter command arguments, deployment results or application logs. Core dumps
are disabled for retrieval. VM root/Docker administrators can still read runtime secrets.

For EIA rotation, obtain the replacement from EIA, update `eia-api-key`, then run a normal
release (or rerun the latest deployment) to fetch a complete generation and safely recreate
the worker. `docker compose restart` alone does not refresh its mounted generation.
Key Vault does not issue or rotate the upstream EIA credential. Initializer passwords are
required for a fresh installation; the EIA key is optional for login and existing data.
Do not print any vault values in Azure Run Command, Actions logs, screenshots or chat.

## Release contract

GitHub Actions publishes the accepted image to the dedicated public
`ghcr.io/<owner>/<repo>-vm` package. Before promoting its digest, the workflow completes
an anonymous registry token exchange and requires the exact manifest's
`Docker-Content-Digest` header to match. The image contains audited public source and
dependencies; runtime credentials, operator files and application data are excluded.
The separate Docker context privacy and functional acceptance checks remain mandatory.

Extract the exact commit's source archive under `/opt/arkham/releases/<full-commit-sha>`.
Build and publish an image with OCI label `org.opencontainers.image.revision` equal to that
SHA. Invoke as root:

```sh
bash /opt/arkham/releases/<sha>/deploy/release.sh <sha> ghcr.io/<owner>/<image>@sha256:<digest>
```

Operators invoking `release.sh` with a separate private GHCR image may supply
`GHCR_USERNAME` and a short-lived `GHCR_TOKEN` through Azure Managed Run Command
**protected parameters**. That optional operator path uses an ephemeral
root-only Docker configuration, pulls the immutable digest, then removes that configuration
and unsets the token before Compose. It never writes a registry credential to persistent
Docker configuration. A local `docker save` archive can optionally be a third argument,
but the inspected image must still match the supplied immutable reference and revision.

The release script serializes upgrades with `flock`, downloads images before pausing the
old app, closes HTTP enqueueing, and checks the durable refresh queue. A queued or running
refresh defers the upgrade with exit 75 and restarts the old app while leaving its worker
alone. Direct operator ingestion and separately launched workers also gate migration.
Run the release again after that work finishes.

Initialization applies Django migrations and seeds default accounts only during the first,
never-served bootstrap. Existing databases never trigger seeding, including when every
account was deliberately deleted. A failed first bootstrap can be retried before serving.
When an existing database needs a migration, init first makes a consistent private SQLite
backup using SQLite's backup API, including committed WAL. A release requiring no migrations
does not make a full database backup.

Private release result files record schema fingerprints and hashes/counts of every table's
rows. Without a schema change every existing row must remain identical before the new app
starts. Schema changes must preserve every pre-existing table's row fingerprints except
Django's applied-migration metadata, as well as the complete catalog. Dropping a table or
changing a row representation therefore requires explicit review.
Backups and result files remain local and are not a substitute for off-machine recovery.

The new app must pass its internal health check, public certificate-validated HTTPS, exact
`X-Outage-Revision`, and worker liveness before an atomic `/opt/arkham/current` symlink update.
`previous` retains the prior compatible source release. After the one-time migration and
retirement of legacy secret files, the old environment-based release is not a rollback
target; the next Key Vault-based release establishes a compatible previous version.
An ordinary failure restores the prior image
when schema and applied migration history remain compatible. A
schema-changing failure keeps the data intact and requires operator review instead of
automatically downgrading the schema or restoring an older database over user changes.

Upgrades briefly pause web traffic. Public process health does not prove that analytical
data has been ingested. The protected readiness endpoint additionally checks publication
and query-runtime availability. Never use `docker compose down --volumes` for this stack.
Monitor the retained 30 GB OS disk and remove superseded **images/source releases** only
after identifying current and previous references; deployments do not prune persistent data.

## Security operations

Required security checks audit the exact Python and npm dependencies, Git history for
secrets, and the accepted container image. Fixable HIGH/CRITICAL image vulnerabilities
block release. Vendor-unfixed findings remain visible for review. Scheduled scans and
Dependabot pull requests support maintenance; updates still pass the protected PR path.
The VM's Ubuntu security-update timers remain enabled, with no forced reboot added by
these deployment scripts.

Public HTTPS liveness is checked every fifteen minutes, on main-branch pushes and manually.
It checks process health, a valid deployed revision, TLS and HSTS, while allowing the older
healthy release to serve during CI. It does not require an analytical publication or restart
services. GitHub schedules are best effort: runs can be delayed or dropped, and public
repositories can have schedules disabled after sixty days without activity. Check Actions
notification preferences for failures; this is not a continuous availability guarantee.

See [backup and restore](backup-restore.md) for separately provisioned encrypted off-host
recovery and the [security policy](../SECURITY.md) for access and disclosure procedures.
