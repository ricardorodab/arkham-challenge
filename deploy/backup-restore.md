# Encrypted off-host backup and isolated recovery

These tools add backups without changing the production image, Compose stack or GitHub
deployment identity. Cloud resources and host installation require operator coordination.
The normal backup never loads a recovery private key. No storage account key, SAS, PAT,
GitHub token or Azure client secret is used by the backup process.

## Storage and host prerequisites

Use a dedicated StorageV2 Hot LRS account and a private `arkham-backups` container. Disable
shared-key authorization and public blob access, require HTTPS/TLS 1.2+, and retain Azure
service-side encryption. Select only the VM subnet through its `Microsoft.Storage` service
endpoint, with firewall default deny and no trusted-service bypass. A same-region VM public
IP rule is insufficient; use the [selected subnet rule](https://learn.microsoft.com/en-us/azure/storage/common/storage-network-security-virtual-networks).

Grant the VM identity a custom role scoped to this exact container, with only Blob read and
write data actions. Do not grant container administration, delete, account-key access or
user-delegation-key generation. Configure versioning, seven-day blob/container soft delete
and lifecycle expiration after 30 days, including old versions; apply it to `backups/` and
`drills/`. No-delete does not prevent overwrite. The implementation uses unique names and
conditional commits, while retained versions provide recovery from a compromised writer.
No irreversible locked immutability policy is installed.

Before assigning any identity data access, install and verify the metadata rule. Docker
bridge containers must not reach `169.254.169.254`; the host still needs that address for
managed identity. `host-security.sh` adds a DOCKER-USER rejection and a Docker startup hook
to reapply it after daemon restart. Verify denial from **each production container** and a
successful host identity request before granting data access. Root-authorized host-network
or privileged containers can bypass host boundaries and must not be used for application
workloads. The backup/restore drill container uses `--network none`.

Install `age`/`age-keygen` from the Ubuntu repository before running:

```sh
bash /path/to/reviewed/deploy/host-security.sh install
```

The installer copies reviewed operational scripts into root-only `/opt/arkham/operations`,
independent of future `/opt/arkham/current` promotions. Re-run it after reviewing operational
script updates. It creates hourly backup eligibility/retry and half-hourly freshness timers.
It does not create storage resources, grant identities or configure backup recipients.

## Recovery key and configuration

On the owner's Windows computer, use a verified official age release and run
`backup-key.ps1`. It creates a dedicated key outside Git under a user-only ACL and tests a
local encrypt/decrypt challenge. Existing directories/keys are never overwritten. Keep an
additional protected offline copy of the private recovery key. Losing every copy makes old
encrypted backups unrecoverable. Do not send this key through Run Command, GitHub, chat,
the VM, or the backup container. Transfer only the public `age1...` recipient.

Create root-owned mode-0600 `/etc/arkham/backup.json`:

```json
{"account":"YOURACCOUNT","container":"arkham-backups","recipient":"age1PUBLICRECIPIENT"}
```

Only public configuration belongs in that file. The account/container must match the
network and role configuration. Backup requests acquire short-lived Storage audience tokens
from the host's managed identity and keep them in memory. HTTP errors expose only status,
never response bodies or bearer tokens.

## Capture, retention and monitoring

`backup-run.sh` uses the deployment lock, stops HTTP enqueueing, defers while a refresh is
queued/running, then stops the worker and takes both writer locks. SQLite's backup API
captures committed WAL from both databases while all writers are excluded. Every registered
historical snapshot/model is included, preserving references from saved views and job
history. Mutable metadata/config files are copied. Contractually immutable Parquet may be
hard-linked on the same filesystem; a copy is used otherwise. Sealed hashes are computed
while quiesced and checked again as bytes enter the archive. A hard link alone does not
freeze bytes. Any later mutation fails the backup before Blob commit.

The app/worker resume before compression, encryption and upload. The archive streams through
age into bounded Blob blocks without a complete plaintext/encrypted archive on the host.
The stream is committed only after age exits successfully and sealed hashes match. The
remote encrypted object is checked against its size/checksum and server-encryption header
before an atomic private success receipt is written. Daily eligibility starts after 20
hours; hourly retries cover active-refresh deferrals or transient failures. Each capture
briefly pauses web traffic. A 30-hour freshness failure is an operational alert, not proof
that a recoverable backup exists forever.

Included: web users/password hashes/sessions/jobs/saved views, catalog, registered immutable
publications, run metadata, the Django signing key, hostname, exact image digest and required
release configuration. Excluded: initial plaintext account passwords, EIA key, Caddy keys,
locks, query scratch, unfinished candidates and old migration backup files. Caddy can issue
new certificates; reconfigure the EIA key privately after recovery. The archive and signing
key are encrypted using age in addition to [Azure encryption at rest](https://learn.microsoft.com/en-us/azure/storage/common/storage-service-encryption).

Inspect `systemctl status arkham-backup.service arkham-backup-check.service`, timer status and
their journals. Run `python3 /opt/arkham/operations/backup.py check` for a remote receipt and
freshness check. Root/private logs expose counts, revision, encrypted object names and safe
errors. Monitor free disk, failed timer units and the Azure lifecycle policy. The freshness
unit deliberately fails when a successful remote object is stale/missing/mismatched;
notification transport is configured separately by the operator.

Disk guards preserve at least 2 GiB of free space on the destination filesystem. SQLite
snapshots and mutable/fallback copies preflight their required bytes; hard links do not
reserve a second complete Parquet copy. Downloads preflight encrypted Content-Length, and
restore extraction preflights the manifest's total expanded bytes while the encrypted
download remains on disk. Chunked writes recheck the remaining budget to detect concurrent
disk consumption. A failed budget check stops before further writes or backup commit and
never marks a partial restore verified. The 30 GiB archive limit is a format safety ceiling,
not permission to consume the production disk's remaining capacity.

## Restore boundaries and drill

`restore.py` downloads and authenticates an encrypted artifact into a **new** disposable
directory, rejecting existing targets, production paths, symlinks, traversal, duplicate/link
members, unexpected files and excessive sizes. It checks sealed file hashes, SQLite
integrity/foreign keys, row fingerprints and catalog registrations. Failed/incomplete trees
remain private and marked incomplete. It never initializes, seeds, migrates, promotes or
overwrites a production workspace. A real recovery cutover is a separate reviewed action.

Run `restore-drill.sh` as root for an actual VM snapshot/upload/download/decrypt/restore
exercise. It generates a **separate temporary** age key in `/run`, captures a fresh backup
encrypted to that temporary public recipient under the `drills/` prefix, and restores it to
`/srv/arkham-restore-tests/drill-...`. It starts the exact locally available image digest
with isolated restored mounts, no network, no published port, no worker/EIA, no init/migrate,
then checks internal revision health and account database readability. It removes only its
own verified scratch paths, container and temporary key; cloud drill artifacts expire by
lifecycle because the VM cannot delete blobs.

**The VM drill does not test the real off-host recovery key or decrypt a production backup.**
The Windows challenge tests that key pair locally. Together they validate separate portions
of the procedure; periodically perform an owner-controlled recovery of a production backup
in a separately approved isolated environment. Never transfer the real recovery private key
to the production VM. LRS storage protects against losing this VM/disk; it does not establish
cross-region disaster recovery. RPO/RTO are measured only by actual successful captures and
restore drills, not guaranteed by these timers.

Age authenticates encrypted bytes, not the sender's identity: anyone with the public
recipient can encrypt a new archive. The private container/identity permissions and retained
versions remain part of the trust boundary. Restore accepts only the trusted
`ghcr.io/ricardorodab/arkham-challenge-vm` namespace; the VM drill additionally verifies the
local image's exact revision and GitHub source labels before running it. Do not use arbitrary
archive-provided images or treat encryption alone as release provenance.
