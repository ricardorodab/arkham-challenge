# Structured browse scale verification

The production `run_browse` execution path was measured on **3,000,000 synthetic `generator_daily` rows** in a **107,561,109-byte Parquet file (102.6 MiB)**. This exceeds the SQL sandbox's separate 64 MiB buffered-input ceiling. The schema is the unchanged nine-column dataset contract; the file SHA-256 was `47099dbf7a671a75b421ff5aaed05d7986ea631dbd6074bc4faafdac3d524bf5` on both platforms.

These are local measurements, not latency guarantees or a concurrent-user load test. Fixture creation and expected-checksum calculation warmed filesystem caches. **Cold means a verification-LRU miss, including full streaming SHA-256; it does not mean cold disk I/O.** No host/VM caches were evicted or globally tuned. Every request starts a fresh isolated engine process. Timings include the production supervisor, integrity work, child startup, query and bounded result transport; HTTP authentication/rendering overhead is outside this benchmark and covered separately by API/browser/Docker tests.

| Operation, 100 visible rows | Windows AMD64 | Linux Docker x86_64 |
| --- | ---: | ---: |
| First page, verification cache miss | 0.678 s | 0.921 s |
| First page, verification cache hit | 0.386 s | 0.620 s |
| Second seek page | 0.421 s | 0.619 s |
| Deep seek around row 2,000,000 | 0.386 s | 0.820 s |
| Non-primary-key column sort | 1.052 s | 1.372 s |
| Global literal substring scan, absent term | 3.650 s | 4.032 s |

Python was **3.14.2** and native DuckDB **2.0.0**. All twelve operations succeeded. Nonempty results contained 101 rows including the private continuation sentinel and at most **22,122 bytes**; the absent-term scan returned zero rows and 408 bytes. The API removes the sentinel, bounds complete responses to 2 MiB and permits at most 1,000 visible rows per page. No complete result set or Parquet byte array is transported to the browser.

Peak child RSS was sampled every 5 ms: **77.6 MiB on Windows**, **148.0 MiB on Linux**. Sampling can miss short peaks; these observations are separate from enforcement. Windows retains the 512 MiB Job Object cap. Linux read back its actual **512 MiB hard address-space limit** on every operation; maximum observed virtual peak was **322.4 MiB**. Both retain one DuckDB thread, the 256 MB engine memory setting, six CPU seconds and eight wall-clock seconds. No input/resource cap was raised to make this fixture pass.

The preserved EXPLAIN text shows a **Top N, Top: 101** operator over a Parquet scan. Seek pagination avoids OFFSET-dependent candidate growth, but it does not introduce an index. The deep-page timing is specific to this fixture's layout/statistics. Global lower/cast/substring search remains scan-heavy, and larger or less selective workloads can exceed the bounded execution budget. There is no per-page matching COUNT scan.

## Reproduce

Use [the benchmark script](../tests/performance/browse_benchmark.py) with an isolated directory and the project's installed dependencies:

```powershell
$browseRun = Join-Path $env:TEMP ("arkham-browse-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -B tests/performance/browse_benchmark.py --directory $browseRun --rows 3000000 --output (Join-Path $browseRun "results.json")
```

For Linux, use the built production image with no network, a read-only root filesystem, dropped capabilities, `no-new-privileges`, a bounded `/tmp`, and a dedicated synthetic named volume. Copy or pipe the same script into that disposable environment, keeping its directory separate from all operator volumes. The script invokes the installed production reader, records response/time/memory observations and saves EXPLAIN evidence. No EIA key, live refresh, global cache eviction or external database is involved.

See [the API contract](API_CONTRACT.md#catalog-and-previews) and [the trusted reader's security boundary](QUERY_SECURITY.md#structured-browsing-has-a-separate-trusted-reader).
