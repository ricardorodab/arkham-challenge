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
trusted bootstrap to set 512 MiB RLIMIT_DATA, six seconds RLIMIT_CPU, no core or
file output and 64 file descriptors before exec; this is a data-allocation
ceiling, not an RSS measurement. WebAssembly memory has a separate hard 256 MiB
ceiling and the V8 old heap is limited to 96 MiB. One baseline compilation task
and disabled optimizing tier-up keep compilation within the Windows limit.
The Python supervisor caps output and applies an eight-second wall deadline,
kills and reaps the process, and drops inherited application environment values.

The WASM import/capability boundary is the isolation mechanism. Process limits,
DuckDB configuration locks and the API SQL grammar are additional protections.
This is not a Docker container, Windows AppContainer, or an assertion that the
Node permission system protects against arbitrary malicious JavaScript. The
JavaScript shim and installed packages are trusted application code; submitted
SQL is never evaluated as JavaScript. Dependency changes require repeating the
engine-level canary, network, configuration and resource tests.
