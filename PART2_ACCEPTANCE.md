# Part 2 acceptance — data model and verification

This delivery implements Part 2 of the supplied challenge, including its five extras. It builds on the real Part 1 extracts. The backend service, authentication, arbitrary user SQL and web application remain Parts 3–4; the local model adapter is ready for trusted backend code.

## Requirement evidence

| PDF requirement | Implemented behavior | Evidence |
| --- | --- | --- |
| Schema, ER, PK/FK, data types | Ten datasets, 113 columns and 12 declared/validated FKs; text entity IDs, daily `DATE`, exact decimal measures | `docs/DATA_MODEL.md`, `docs/ER.mmd`, `model_contracts.py`, builder/verification tests |
| Duplicates and invalid values | Identical raw duplicates may collapse; conflicting keys, invalid identity/date/units/types and precision loss fail; nullable/outlier measures survive with flags | Ingestion tests and model structural/edge-case tests; all real fact rows independently compared with raw |
| US-08 implemented/documented | National outage/capacity, Python Decimal precision 28, HALF_UP, independently rounded share/percentage; undefined denominator remains null | `fleet_daily`, catalog metric definition, `docs/DATA_MODEL.md`, independent precision-70 recomputation of all 7,243 dates |
| Parquet available to backend | Ten immutable ZSTD Parquet datasets; logical metadata and explicit scoped DuckDB adapter | `model_store.analytical_catalog`, `open_model_connection`, CLI and wheel checks |
| Reconcile at least 30 days | 2026-09-01 through 2026-09-30: 30 national days, 1,650 facility-days, 2,850 generator-days, all capacity/outage MW gaps exactly zero | `FINDINGS.md`; named daily national/facility and discrepancy queries in packaged `reconciliation.sql` |
| Report nonmatches with values and explanation | Percentage differences are reproduced as rounding diagnostics; signed/absolute MW and percentage gaps and coverage counts are retained | September 3 concrete values and full date/entity results; no invented MW disagreement |
| At least three real anomalies and handling | Indian Point zero denominator with positive percentage; River Bend internal observation gap; Grand Gulf consecutive capacity steps | `FINDINGS.md`, fixed historical SQL probes, three actual stored DQ event records |
| Reproducible findings | 18 named SELECTs, bound dates, SQL hashes, model/source provenance, exact JSON decimals | `model report`, packaged SQL, pinned report command in `FINDINGS.md` |
| Partition strategy | Sorted single-file dataset snapshots at measured small volume, date-first keys/row groups; explain when date partitions become useful | `docs/DATA_MODEL.md`, decision 17 |
| Historical/versioned data | Immutable raw/model snapshots, one transactional publication authority, model/metric versions and source hashes | Live automatic refresh, model history, crash/rollback/stale-source tests, retained v1 verification |
| Larger-volume optimizations/trade-offs | Streaming batch processing, bounded DuckDB memory, compressed columnar storage; future partitioning, compaction and incremental model costs discussed | `modeling.py`, `docs/DATA_MODEL.md` scaling section |
| Metric definitions in catalog | Versioned formula, operands, units, null/rounding policy; metadata filtered by persona | `model catalog`, catalog tests and installed-wheel check |
| Automated anomaly checks | Deterministic quality events on every build, including all three primary examples; structural failures block publication | `data_quality_events`, automated evidence SQL, DQ/rollback tests |

## Published real data

At acceptance, the current raw snapshot is `1e6f80a89cb4455d949d541a6fe68e2a`; its model is `9811a42ac9a34b4581d00a9bbd5d5d9b`, model version **2**, US-08 metric version **1**. The local catalog binds them in the same publication transaction.

| Dataset | Rows |
| --- | ---: |
| `dim_date` | 7,243 |
| `dim_facility` | 66 |
| `dim_generator` | 107 |
| `national_daily` | 7,243 |
| `facility_daily` | 438,119 |
| `generator_daily` | 714,657 |
| `fleet_daily` | 7,243 |
| `facility_reconciliation_daily` | 438,119 |
| `national_reconciliation_daily` | 7,243 |
| `data_quality_events` | 620 |

The source facts contain **1,160,019 observations**, covering 2007-01-01 through 2026-10-30. Current source Parquet occupies 46,388,292 bytes and the ten modeled Parquet files 44,086,438 bytes. Identity counts cover historical observations, not the number of currently operating facilities/generators.

The 620 review events comprise 58 capacity changes, 184 observed-span entity gaps, 182 source nonpositive-capacity flags, 182 zero-capacity events and 14 zero-capacity/positive-percentage events. Events can describe the same observation under different rules or grains; they are not 620 proven source errors.

`FINDINGS.md` pins the earlier retained v2 model `e5721cad365d47beb0a8a322199a3138`, source `981a8f4ed5214ef6be4ad3cc5b4eee01`. Its ten logical dataset hashes equal the current model's. The first development model `248163e1c61e497d870dd06297a750ca` is also retained. Its original unit-label contract is frozen and its catalog emits a warning; its numeric content matches v2. Historical artifacts were not edited to correct metadata.

## Observed reconciliation and anomalies

September's 30-day comparison found **zero capacity or outage MW differences** for generator-to-facility, generator-to-national and facility-to-national comparisons. The full-history checks likewise found zero MW gaps. The report includes every daily national and facility comparison, plus a separate nonzero-gap query, whose observed result is empty.

On 2026-09-03, national capacity is 97,620.5 MW and outage 1,507.338 MW. EIA reports 1.54%; the exact operand ratio is approximately 1.544079368575248%. Capacity-weighted published facility percentages give approximately 1.544451810838912%. These are rounding differences, not disagreeing MW totals. Exact scaled-integer residual checks avoid interpreting floating-point noise as a source discrepancy.

Three distinct primary anomalies are reproduced with date/entity/value evidence and product handling in `FINDINGS.md`:

1. **Indian Point 2 / facility 2497 / generator 2:** 91 zero-capacity days in 2020 Q1; 14 days still report positive generator outage percent, including 11% on March 31 with capacity/outage both zero. Preserve the observations; derived denominator-based percentages remain null and flagged. NRC evidence shows that zero MW does not establish physical shutdown.
2. **River Bend / facility 6462 / generator 1:** 120 missing days from both detail routes, January–April 2013, bounded by actual observations. NRC reports operating output within the gap. Preserve missingness and emit a coverage event rather than invent rows. All-grain aggregate agreement cannot establish complete entity coverage.
3. **Grand Gulf / facility 6072 / generator 1:** capacity falls from 1,463 to 1,190 MW on 2012-04-01, with the reverse step on October 1. Preserve the daily denominator and flag the consecutive change. Seasonal capacity is a plausible context, not a proven route-specific cause.

The `automated_quality_event_evidence` query returns the three corresponding stored generator events. The report does not depend on fabricated fixtures. Synthetic tests exercise cases absent from the real extract, including null measures and generator orphans; those tests are not presented as source discoveries.

## Refresh, integrity and failure evidence

A real recent ingestion reread October 1–30 in nine requests with zero retries. It observed zero added/changed/deleted observations in all three routes, built the model automatically and published raw/model IDs listed above. All three raw logical hashes and all ten modeled logical hashes remained unchanged; earlier versions stayed available.

The invocation ran from 07:15:35 through 07:19:18 UTC on 2026-11-01, approximately 223 seconds including model validation/publication on this machine. Its immutable raw manifest records 22.916 seconds through raw preparation; model history records its separate start/finish. This is one observed run, not a performance guarantee. The success log now measures the complete invocation.

Independent verification of the current model checked:

- Every source/fact row through full natural-key joins: no missing rows or measure/name/flag/source-hash mismatches across 1,160,019 observations.
- All 7,243 US-08 results using an independent precision-70 Decimal calculation: no differences.
- All 438,119 facility and 7,243 national reconciliation rows, reaggregated directly from raw: no count, null-count, sum, signed/absolute gap or weighted-percentage mismatches.
- All 91 zero-capacity facility weighted ratios remain undefined.
- SQLite integrity, current source/model coherence, successful job states and exact source SHA/count bindings.
- Complete historical v1 verification, including all ten dataset hashes, 12 foreign keys and its frozen semantic metadata.

Publication tests inject invalid models, wrong source bindings, stale rebuilds, failed commits and lost diagnostic writes. Three real subprocess-death tests terminate after raw rename, after model rename, and immediately after the joint commit. Recovery preserves both prior pointers before commit and both new pointers after commit; the OS lock is released by process death.

## Reproduce locally

Install with the README commands. A fresh clone has no data or API key: first ingest the real source using `EIA_API_KEY`. New ingestion automatically builds the model. A directory containing only an earlier Part 1 snapshot can be upgraded offline:

```powershell
.\.venv\Scripts\python.exe -m outage_explorer model build --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer model verify --data-dir data
.\.venv\Scripts\python.exe -m outage_explorer model catalog --data-dir data --persona analyst
.\.venv\Scripts\python.exe -m outage_explorer model history --data-dir data --limit 10
.\.venv\Scripts\python.exe -m outage_explorer model report --data-dir data --start 2026-09-01 --end 2026-09-30 --output work/part2-report.json
```

To reproduce the exact retained finding bundle on this machine, add `--model-id e5721cad365d47beb0a8a322199a3138` to `model report`. A newly ingested clone receives different IDs and may observe later EIA revisions. Inspect its report provenance rather than assuming historical source bytes never change. Reports and model inspection need no API key.

## Automated acceptance results

- Python 3.14.2: **302 passed, 1 skipped**, 106.51 seconds.
- Python 3.12.14: **302 passed, 1 skipped**, 116.17 seconds.
- Fresh Python 3.14 CLI/pipeline run after the elapsed-log correction: **63 passed**, 47.08 seconds.
- Fresh checkout of `f3b7cd615120a704942819cf8cbd811e6c7ad84c`, newly created isolated Python 3.12.14 environment and locked dependencies: **302 passed, 1 skipped**, 107.06 seconds. No API key or local data was copied. Recorded fixture hashes, 18 packaged SELECTs, v1/v2 profiles, Ruff and `pip check` passed; the clone's Git tree remained clean. The subsequent test-file edit only corrects its module description from two crash boundaries to three.
- Ruff lint/format and dependency checks pass. The only skipped test requires Windows symlink privileges unavailable on this host. Linux CI is configured but no remote CI execution is claimed.
- A built `outage-explorer 0.2.0` wheel was installed with pinned DuckDB 2.0.0 into an isolated environment. From outside the repository, `python -I` loaded its two SQL resources, 18 SELECTs and frozen v1 contract, then reproduced the pinned catalog/report. Report content matched the canonical report except generation time.
- Actual supplied-key absence passed across all 137 Git objects then present, 59 tracked files, 15 persisted data JSON files, 28 work-root files including logs/reports, SQLite bytes, and 39 Parquet files with 162 decompressed VARCHAR columns. `key.txt` is ignored and untracked. Counts identify that audit's scope, not future edits.

Local machine evidence is retained under ignored `work/`: `part2-refresh-evidence.json`, `part2_model_current_independent_audit.json`, `part2_findings_final_validation.json`, `part2-current-verify.json`, `part2-wheel-acceptance.json`, `part2-clean-checkout-evidence.json` and `part2-secret-audit.json`. The committed implementation, queries and tests provide the reproducible mechanism; generated datasets, operational state and credentials are not committed.

## Remaining limits

EIA pagination is live, not a simultaneous source transaction. A 30-day overlap cannot discover every older revision; source identity gaps outside observed spans cannot be inferred without an external operating roster. Review flags do not diagnose every upstream cause. Full rebuilds and complete hash verification favor correctness at this volume and will need measured incremental/partitioning work at much larger scale. No automatic retention, off-machine backup, authenticated API or hostile-SQL execution boundary is claimed by Part 2.
