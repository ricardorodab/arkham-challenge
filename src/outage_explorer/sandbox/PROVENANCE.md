# Query runtime assets

`parquet-v1.5.4-wasm_eh.wasm` is the official DuckDB Parquet WebAssembly
extension for the `wasm_eh` platform, version `v1.5.4`.

- Source: https://extensions.duckdb.org/v1.5.4/wasm_eh/parquet.duckdb_extension.wasm
- Download verified: 2026-11-01
- Bytes: 3,218,307
- SHA-256: `4845705bbd69fc9ad52878d96a505c73cae4a6c509822079cc2413e5eb437f95`
- License: MIT, https://github.com/duckdb/duckdb/blob/main/LICENSE

The pinned npm package is `@duckdb/duckdb-wasm@1.33.1-dev57.0`; its embedded
engine reports `v1.5.4`. `package-lock.json` fixes the npm dependency graph and
download integrity. This extension is supplied from the package asset by a
strict bootstrap-only loader, never downloaded by a query process.

The worker uses a custom BUFFER-only runtime rather than the npm package's
NODE_RUNTIME, which has host filesystem capabilities. SQL sees only the selected
Parquet byte buffer. Node's permission mode independently denies all host file
writes, child processes, workers, addons, inspector and WASI; read grants cover
only installed runtime files and these exact application assets. No dataset host
path or application environment secret reaches the worker.

Windows uses a Job Object with a hard 512 MiB process/job memory limit, six
seconds of user CPU time, one process and kill-on-close. POSIX uses a separate
trusted bootstrap: default/compiler mode keeps 512 MiB RLIMIT_DATA; the trusted
parent selects `--engine` for Node, allowing 1,024 MiB on Linux and 512 MiB on
other POSIX systems. Both modes retain six seconds RLIMIT_CPU, no core/file
output and 64 file descriptors before exec.

The Linux engine allowance budgets 512 MiB for the tested Node 24.13.0 x64 V8
JavaScript code-cage reservation plus 512 MiB for other DATA-accounted mappings.
V8 defines the x64 maximum code-range size in
[globals.h](https://github.com/nodejs/node/blob/v24.13.0/deps/v8/src/common/globals.h#L471-L475)
and precommits the cage as writable outside Windows/iOS in
[code-range.cc](https://github.com/nodejs/node/blob/v24.13.0/deps/v8/src/heap/code-range.cc#L226-L256).
Linux counts this mapping against RLIMIT_DATA even when pages are not resident.
The 1,024 MiB ceiling is one shared DATA allowance, not an RSS ceiling or an
equivalent of the Windows hard process-memory limit.

WebAssembly linear memory has a separate hard 256 MiB ceiling, V8 old heap is
limited to 96 MiB and WASM committed code to 128 MiB. Linux additionally sets
`--wasm-max-code-space-size-mb=128` for individual WASM code-space reservations
and `--wasm-max-initial-code-space-reservation=32` for each initial reservation.
V8's [initial-reservation clamp](https://github.com/nodejs/node/blob/v24.13.0/deps/v8/src/wasm/wasm-code-manager.cc#L2483-L2490)
reduces up-front mapping; [additional code spaces](https://github.com/nodejs/node/blob/v24.13.0/deps/v8/src/wasm/wasm-code-manager.cc#L717-L758)
can be allocated later within the separate hard limits. The 32 MiB setting is
not a total committed-code or linear-memory ceiling; these WASM reservations
are distinct from the JavaScript code cage. One baseline compilation task and
disabled optimizing tier-up keep compilation within the Windows limit.
The Python supervisor caps output and applies an eight-second wall deadline,
kills and reaps the process, and drops inherited application environment values.

The WASM import/capability boundary is the isolation mechanism. Process limits,
DuckDB configuration locks and the API SQL grammar are additional protections.
This is not a Docker container, Windows AppContainer, or an assertion that the
Node permission system protects against arbitrary malicious JavaScript. The
JavaScript shim and installed packages are trusted application code; submitted
SQL is never evaluated as JavaScript. Dependency changes require repeating the
engine-level canary, network, configuration and resource tests.
