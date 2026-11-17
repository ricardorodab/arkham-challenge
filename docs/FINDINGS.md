# Data findings: agreement, rounding and source anomalies

**The retained data agrees in MW across national, facility and generator levels, but agreement does not prove complete coverage.** Three cases explain the distinction:

| Finding | Evidence | Interpretation |
| --- | --- | --- |
| Indian Point 2 | 91 zero-capacity days; 14 nonzero reported percentages | The derived percentage has an invalid denominator. |
| River Bend | 120 days missing from both detailed sources | Matching totals can conceal missing entities. |
| Grand Gulf | A 273 MW capacity step with unchanged outage percentage | Daily capacity changes affect the numerator and denominator. |

These are historical findings, not statistics from today's publication. During this update, all 18 packaged evidence queries were rerun read-only against the pinned local model; their results still match. No EIA download or production change was made. The cited NRC reports and EIA context were checked against primary-source pages/indexed content; they do not establish the causes of API anomalies.

## Read outage values and warnings correctly

A **100% outage can be valid**: all reported capacity is offline that day. This measures unavailable capacity in MW, not electricity generated, energy lost in MWh or capacity factor. A missing observation, null value and reported zero are different.

`quality_flags = []` means no configured ingestion warning fired for that row. It does **not** establish complete coverage or agreement between datasets. Model-level checks produce separate `data_quality_events`; a warning identifies a review condition, not its physical cause.

The [model guide](DATA_PIPELINE_AND_MODEL.md) defines the schema and metric; [DECISIONS](DECISIONS.md) explains preservation choices; [NOTES](NOTES.md) records verification scope.

## Reproduce the evidence

```powershell
.\.venv\Scripts\python.exe -m outage_explorer model report --model-id e5721cad365d47beb0a8a322199a3138 --start 2026-09-01 --end 2026-09-30 --output work/part2-report.json
```

This requires the retained, ignored local model; a fresh clone does not contain it. The report records source bindings and query checksums. It runs trusted read-only native DuckDB queries from [reconciliation.sql](../src/outage_explorer/analytics/sql/reconciliation.sql) and [part2_findings.sql](../src/outage_explorer/analytics/sql/part2_findings.sql), outside the restricted user SQL workspace. Windowed queries use the supplied dates; historical probes retain literal dates. SQL comment changes affect future report hashes, not historical receipts.

| Provenance | Retained value |
| --- | --- |
| Source snapshot | `981a8f4ed5214ef6be4ad3cc5b4eee01` |
| Model | `e5721cad365d47beb0a8a322199a3138` |
| Coverage | 2007-01-01–2026-10-30 |
| Versions | Model 2; fleet metric 1. Model 2 corrected percentage unit labels, not values or formula. |
| Source rows | National 7,243; facility 438,119; generator 714,657; total **1,160,019** |

Source measures use `DECIMAL(24,9)`; IDs are strings, with generator identity `(facility_id, generator_id)`. Capacity belongs to its observation date, not the latest dimension record.

## MW totals agree; coverage remains a separate question

September 1–30, 2026 contains 30 days, 55 observed facilities/day and 95 generators/day: 1,650 facility comparisons and 2,850 generator observations.

| Comparison | September keys | Full-history keys | Nonzero gaps / maximum absolute gap |
| --- | ---: | ---: | --- |
| Generators → facility | 1,650 | 438,119 | 0 / 0 MW |
| Facilities → national | 30 | 7,243 | 0 / 0 MW |
| Generators → national | 30 | 7,243 | 0 / 0 MW |

Both capacity and outage agree exactly, without a tolerance. Full-history observed comparisons have both sides and no null MW preventing comparison. `mw_discrepancies` returns **zero rows** for September. Shared omissions still pass these checks, as River Bend demonstrates.

Queries: `daily_national_reconciliation`, `daily_facility_reconciliation`, `full_history_mw_summary`, `mw_discrepancies`.

## Percentages differ because of rounding

Aggregate percentages from MW: `100 * SUM(outage_mw) / SUM(capacity_mw)`. Averaging already-rounded published percentages is a separate diagnostic.

On **2026-09-03**, all three levels total **97,620.5 MW capacity** and **1,507.338 MW outage**:

| Percentage | Value (%) | Difference from exact ratio (percentage points) |
| --- | ---: | ---: |
| Exact MW ratio | 1.544079368575248… | 0 |
| Published national | 1.54 | −0.004079368575248… |
| Capacity-weighted published facility | 1.544451810838912… | +0.000372442263664… |
| Capacity-weighted published generator | 1.544079368575248… | 0 |

All 714,657 generator rows have integer reported percentages and satisfy `capacity * percentage = 100 * outage`. Zero-capacity cross-products are degenerate and cannot validate a ratio.

All 30 national and 1,650 facility September rows match the MW ratio rounded to two decimals using `ROUND_HALF_UP`: **zero failures**, although 30 national and 228 facility percentages differ from the unrounded ratio. Scaled-integer checks prove real rounding differences, not floating-point noise: September 3's exact national residual is **−398.230 MW·pp**, and the facility-weighted numerator exceeds the generator-weighted numerator by **36.358 MW·pp**. The SQL documents the exact scaling/tests.

Keep reported and calculated percentages separate. The canonical fleet metric is the same day's published national `outage_mw / capacity_mw`. It uses Python `Decimal`, rounding the unrounded ratio independently to **12-place share** and **9-place percentage**: `0.015440793686`, `1.544079369%`; reported-minus-calculated is `−0.004079369` percentage points. A percentage-point difference is not a relative percent change. This establishes rounding compatibility in the retained snapshot, not universal upstream methodology.

Queries: `exact_percentage_rounding_summary`, `percentage_gap_example`, `generator_exact_arithmetic`.

## Three source anomalies

### Indian Point 2: zero capacity, undefined derived percentage

Facility **2497**, generator **2** has zero capacity/outage for **91 consecutive days, 2020-01-01–2020-03-31**; **14** rows report nonzero generator outage percentages.

| Date | Capacity MW | Outage MW | Generator / facility reported % | Derived % |
| --- | ---: | ---: | --- | --- |
| 2019-12-31 | 1,025.9 | 0 | 0 / 0 | 0 |
| 2020-01-01 | 0 | 0 | 0 / 0 | Undefined |
| 2020-03-30 | 0 | 0 | 10 / 0 | Undefined |
| 2020-03-31 | 0 | 0 | 11 / 0 | Undefined |

[NRC reported 90% reactor power on March 30](https://www.nrc.gov/documents-reports/document-collections/events-reports-associated-with/power-reactor-status-reports/2020/20200330ps); [EIA places retirement in April 2020](https://www.eia.gov/TodayinEnergy/detail.php?id=47776). Reactor power does not establish electrical MW or the missing capacity value. The zero-denominator condition is confirmed; its upstream cause is unknown.

Preserve source percentages and `capacity_non_positive`; derive null for nonpositive capacity. Do not treat `0/0` as 0%, infer retirement, discard rows or backfill 1,025.9 MW without a separately sourced estimate.

Queries: `zero_capacity_summary`, `indian_point_evidence`.

### River Bend: 120 missing days despite operating evidence

Facility **6462**, generator **1** is absent from both sources for **2013-01-01–2013-04-30**. Boundary capacity changes from **979 MW on 2012-12-31** to **969 MW on 2013-05-01**, with zero outage on both dates. Those values cannot determine missing-day measurements.

[NRC reported 100% reactor power on 2013-01-15](https://www.nrc.gov/documents-reports/document-collections/events-reports-associated-with/power-reactor-status-reports/2013/20130115ps). That day's national capacity/outage is **99,613.7 / 6,389.192 MW**; both child sums agree exactly despite zero River Bend rows.

The omission is confirmed; its cause and effect on EIA's intended population are unknown. Establishing completeness requires an external dated membership roster, not observed first/last dates. Keep gaps visible; do not zero-fill or forward-fill them. The model attaches the coverage warning to the first returning observation, **2013-05-01**, without inventing missing facts.

Queries: `river_bend_coverage_gap`, `river_bend_boundary_rows`, `river_bend_aggregate_blind_spot`.

### Grand Gulf: capacity changes while percentage stays constant

Facility **6072**, generator **1**:

| Date | Capacity MW | Outage MW | Reported % |
| --- | ---: | ---: | ---: |
| 2012-03-31 | 1,463 | 1,463 | 100 |
| 2012-04-01 | 1,190 | 1,190 | 100 |
| 2012-09-30 | 1,190 | 0 | 0 |
| 2012-10-01 | 1,463 | 0 | 0 |

The **−273 MW (−18.6603%)** and **+273 MW (+22.9412%)** changes meet `capacity_change`: consecutive calendar days, positive previous capacity, absolute change **≥100 MW AND ≥10%**. The April outage MW decreases with capacity despite unchanged 100% outage.

Across history, **3,096 of 3,580** generator capacity changes occur April 1/October 1. On **2026-10-01**, national capacity rises **97,620.5 → 100,056.7 MW (+2,436.2)** with the identical set of **95 generators**, verified by set differences. Peach Bottom **3166/2** rises **1,264.7 → 1,346.7 MW (+82)**: reported outage stays 3%, while outage rises **37.941 → 40.401 MW**. That smaller change remains visible below the warning threshold.

[EIA distinguishes summer/winter capacities](https://www.eia.gov/tools/faqs/faq.php?id=101&t=3); calendar concentration is consistent with a seasonal basis but proves neither API effective dates nor Grand Gulf's cause. Uprate, correction and seasonal substitution remain possibilities. Retain daily denominators and absolute MW; do not infer a restart or new identity.

Queries: `grand_gulf_capacity_change`, `fleet_capacity_change`, `fleet_identity_change`, `capacity_change_calendar`, `generator_capacity_change_evidence`.

## Review events are annotations, not repairs

`automated_quality_event_evidence` returns warning records for Grand Gulf **2012-04-01**, River Bend **2013-05-01** and Indian Point **2020-03-31**. The model contains:

| Rule | Events |
| --- | ---: |
| `capacity_change` | 58 |
| `entity_coverage_gap` | 184 |
| `source_capacity_non_positive` | 182 |
| `zero_capacity` | 182 |
| `zero_capacity_positive_reported_pct` | 14 |
| **Total** | **620** |

Rules overlap: the 91-day zero-capacity episode appears at facility and generator levels. **620 events does not mean 620 distinct bad rows.** See [event rules](DATA_PIPELINE_AND_MODEL.md#comparing-grains-and-detecting-review-events). Entity-level presentation suggestions do not imply every recommended chart/label is implemented.

<a id="what-the-november-inspection-confirmed"></a>

## Historical inspection and pagination evidence

The November 2, 2026 UTC inspection observed source **`9b3e376033ba44298d86cbf754b85335`**, model **`a9163784f25249189aca6c2877c17889`**, with the same coverage and versions above; identifiers were rechecked November 3 UTC. These are historical receipts, not current publication pointers.

| Dataset(s) | Rows each |
| --- | ---: |
| `national_daily`, `fleet_daily`, `dim_date`, `national_reconciliation_daily` | 7,243 |
| `facility_daily`, `facility_reconciliation_daily` | 438,119 |
| `generator_daily` | 714,657 |
| `dim_facility` / `dim_generator` | 66 / 107 |
| `data_quality_events` | 620 |

All facility/national reconciliation rows had status `exact`. All ten **stored** logical hashes matched the pinned findings model, also confirmed in this update; this is not recomputation of every digest. Historical identity counts do not count today's operating units. The inspection used read-only SQLite/native DuckDB; its immutable catalog open followed verification that no WAL existed, not a method for bypassing live WAL. See [operations](LOCAL_OPERATIONS.md).

The retained October 1–30 refresh manifest records **9 requests** (3 metadata, 3 nonempty pages, 3 empty terminal pages), **0 retries**, **0 added/changed/deleted rows**, and **46,388,292 raw Parquet bytes**. Facility declared **2,850** rows but delivered **1,650**, correctly flagged `declared_total_mismatch`; generator/national totals were **2,850 / 30**. Therefore totals are provenance: pagination continues to an empty page, with full-key ordering and repeated-page safeguards.

National had no source warnings or missing calendar days; facility and generator each had **91 `capacity_non_positive` warnings**. A date present somewhere does not prove complete entity coverage. Open questions remain the expected dated population, River Bend's omission, Indian Point's denominator and capacity-basis effective dates. Preserve evidence while investigating them.
