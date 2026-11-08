# Docker acceptance

Build the production image first, then run this host-side script from any directory:

```console
python tests/docker/context_privacy.py --image arkham:local
python tests/docker/acceptance.py --image arkham:local
```

The host needs Python 3.12+ and Docker with its Compose plugin. `--docker PATH` can select
the CLI executable. The Docker engine must publish ports on this host; remote engines need
their own routing and are outside this local acceptance command.

The harness creates a unique Compose project, random loopback host port and fresh named
volumes. It starts `app` with its real `init` and continuous-worker dependencies, without a
worker profile flag, then repeats initialization to check that existing credentials are
preserved. An Admin job is consumed automatically with no EIA key and must finish with an
actionable configuration failure while the app and worker remain running and no publication
is created. Synthetic source data then passes through
the real ingestion/model pipeline; HTTP checks exercise CSRF, authentication, persona
authorization and exact dates/decimal strings in the real Node/DuckDB-WASM engine.

The harness stops its automatic worker before testing synthetic success and upstream failure
with separate one-shot consumers. Their acceptance-only fixture replaces the upstream EIA
client and delegates to the real local worker command,
job handling, ingestion and publication. Successful and failing refreshes are checked,
followed by bounded SIGTERM, restart and container replacement with persistent credentials,
model references and job history. No EIA key or upstream network request is needed.

`fixture.py` is copied through Docker's API into a disposable project volume, then mounted
read-only at `/acceptance` only for these checks. No host bind mount is required, and it is
excluded from the production image. The harness reads credentials generated in its new volume into memory
for HTTP login and never prints them. It does not use the operator's configuration or data.

The JSON summary contains static stage names and public metadata only. Failure output does
not contain raw HTTP responses or container logs. Cleanup uses `compose down --volumes`
with the harness's unique project name; it never prunes unrelated Docker resources or
deletes host directories. If cleanup fails, the reported project name identifies the
remaining disposable resources.

`context_privacy.py` separately copies only explicit production manifests and current
Git-tracked application source files into a temporary context. It never copies the real
ignored data, configuration or credentials. Eleven synthetic canaries cover environment/key
files, data/state, scratch work, private preparation, nested private source and host dependency
directories. A COPY-only audit build uses the existing image and the exact production
`.dockerignore`; its container checks blocked paths, scans for the unique marker and verifies
required public file hashes. Only its labelled audit image/container and temporary context
are removed. A failing Docker cleanup is reported as a failed audit.

Image checks also verify the nonroot user, pinned Python/npm versions, selected runtime asset
hashes and absence of known local-state paths. The synthetic context audit proves the tested
ignore rules for those paths; it is not a scan of every historical production image layer or
proof that arbitrary secret content placed inside an allowed source file would be excluded.
