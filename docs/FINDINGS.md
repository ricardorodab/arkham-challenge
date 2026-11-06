# What the retained outage observations show, and what they cannot prove

The national, facility and generator observations agree exactly in capacity and outage MW in the retained snapshot. That agreement is useful, but it does not prove that every operating reactor appears in the data. A missing River Bend interval, an Indian Point zero denominator and a 273 MW Grand Gulf capacity step show why coverage, denominator and change checks are separate questions.

Read [the data pipeline and model](DATA_PIPELINE_AND_MODEL.md) for the source contract, formula and full schema. This guide keeps the actual dates, identities, arithmetic, query names and interpretation limits together. [Verification records](NOTES.md) distinguish historical acceptance results from later checks; [design rationale](DECISIONS.md) explains why observations are annotated rather than silently repaired.

## Separate observed evidence from an explanation of its cause

A source row establishes what this retained source reported. A reproducible calculation establishes a relationship among those values. An external NRC or EIA page can provide independent context, but does not automatically explain this API's methodology. A quality event establishes that a deterministic review rule fired; it is not a physical diagnosis.

In particular, a missing observation, a null measure and a reported zero have different meanings. An absence is not a zero outage or a retirement. Capacity is a daily observation, not a fixed characteristic that can safely be copied into missing history. The canonical percentage uses the same day's published national outage and capacity and describes reported offline capacity, rather than generation, daily MWh loss or capacity factor.

The external links below belong to the retained analysis. They were not fetched again during the November 2 local inspection or this documentation rewrite. No new EIA extraction or analytical reaggregation was performed for this rewrite.

## Reproduce the retained analysis

```powershell
.\.venv\Scripts\python.exe -m outage_explorer model report --model-id e5721cad365d47beb0a8a322199a3138 --start 2026-09-01 --end 2026-09-30 --output work/part2-report.json
```

The report executes the packaged application-owned, read-only queries in [reconciliation.sql](../src/outage_explorer/sql/reconciliation.sql) and [part2_findings.sql](../src/outage_explorer/sql/part2_findings.sql). It contains the daily comparisons and concrete entity rows, including an empty result for actual MW discrepancies. Window queries use the supplied dates; historical evidence queries deliberately retain their documented dates. The SQL uses registered logical datasets, with no filesystem paths or API credentials.

The original retained analysis was calculated from source snapshot **`981a8f4ed5214ef6be4ad3cc5b4eee01`**, model bundle **`e5721cad365d47beb0a8a322199a3138`**, model version **2**, covering **2007-01-01–2026-10-30**. There are 7,243 national rows, 438,119 facility rows and 714,657 generator rows. No new EIA download was required. The command pins the immutable model so a later ingest cannot silently change the evidence. Its `work/` output is a future scratch report. [Engineering notes](NOTES.md) describe the historical acceptance and later inspection scopes; private local receipts are not available in a fresh clone. The report and model manifest record its source bindings and checksums. The fleet metric definition remains version **1**; model version 2 corrects percentage unit labels without changing numeric observations or the metric formula.

The source measures are preserved as `DECIMAL(24,9)`. IDs are strings; generator identity is the pair `(facility_id, generator_id)`. Queries use the observation's capacity, rather than a facility's latest capacity. A percentage-point gap is a difference between percentages, not a relative percentage change. A missing observation, a null measure and a reported zero remain distinct.

## What the November inspection confirmed

The retained findings above pin an earlier immutable bundle so their evidence does not change after refresh. A separate **November 2, 2026 UTC** read-only inspection observed source **`9b3e376033ba44298d86cbf754b85335`** and model **`a9163784f25249189aca6c2877c17889`**, model version **2**, metric version **1**, covering **2007-01-01 through 2026-10-30**. Publication identifiers were rechecked read-only on November 3 UTC and still matched that observation. These are dated checks, not a statement that this rewrite queried today's publication references.

| Dataset | Rows observed November 2 |
| --- | ---: |
| `national_daily` | 7,243 |
| `facility_daily` | 438,119 |
| `generator_daily` | 714,657 |
| `fleet_daily` | 7,243 |
| `dim_date` | 7,243 |
| `dim_facility` | 66 |
| `dim_generator` | 107 |
| `facility_reconciliation_daily` | 438,119 |
| `national_reconciliation_daily` | 7,243 |
| `data_quality_events` | 620 |

The three source facts total **1,160,019 observations**. Historical identity counts do not describe the number operating on one current date. Independent SELECTs found all 438,119 facility-reconciliation and 7,243 national-reconciliation rows had `mw_comparison_status='exact'`.

All **ten stored `logical_sha256` values** in that model matched the historical findings model `e5721cad365d47beb0a8a322199a3138`. That establishes a stored-fingerprint comparison, not a recomputation of every logical digest during the inspection. The September 3 metric was also independently selected: capacity `97620.500000000`, outage `1507.338000000`, reported percentage `1.540000000`, share `0.015440793686`, calculated percentage `1.544079369`, reported-minus-calculated gap `-0.004079369` percentage points.

The inspection queried the catalog with a read-only SQLite URI, then queried immutable Parquet through an in-memory native DuckDB connection. Its initial `mode=ro` catalog open failed with “unable to open database file.” After verifying that no `catalog.sqlite3-wal` existed, the recorded inspection used `mode=ro&immutable=1` successfully. This describes that historical method and its precondition; it is not an instruction to bypass live WAL state. See [local operations](LOCAL_OPERATIONS.md) for operating and recovery guidance.

### Why the provider's total cannot terminate pagination

The inspected source manifest described an October 1–30 recent refresh with **9 HTTP requests**, **3 nonempty pages**, **0 retries** and **0 added/changed/deleted rows**. The requests comprised 3 metadata requests, 3 nonempty data pages and 3 empty terminal pages. Raw Parquet totaled **46,388,292 bytes**.

For that window the facility route recorded `source_declared_total=2,850`, although the actual facility population was **1,650 observations**. Its manifest correctly set `declared_total_mismatch=true`. The generator route's total was 2,850 and the national route's was 30. This is concrete evidence for treating the provider's total as provenance and fetching until an empty terminal page, with complete-key sorting and repeated-page safeguards.

The national source had no source flags or missing source-calendar days. Facility observations included **91 `capacity_non_positive` flags** and the declared-total mismatch; generator observations included **91 `capacity_non_positive` flags**. A calendar day appearing somewhere in a grain does not establish that every entity appears that day.

## Thirty-day and full-history reconciliation: no MW discrepancy

The fixed review window is **2026-09-01–2026-09-30**, including 30 calendar days, 55 observed facilities each day and 95 observed generators each day. It yields 1,650 facility-day comparisons and 2,850 generator-day observations. Both capacity and outage reconcile without a tolerance: every child-sum-minus-parent gap is exactly **0 MW**.

| Comparison | September keys | Full-history keys | Nonzero MW gaps in full history | Maximum absolute MW gap |
| --- | ---: | ---: | ---: | ---: |
| Generators → facility | 1,650 | 438,119 | 0 | 0 |
| Facilities → national | 30 | 7,243 | 0 | 0 |
| Generators → national | 30 | 7,243 | 0 | 0 |

All full-history comparison keys have both published parent and observed child aggregates, with no null capacity/outage measure preventing comparison. This checks the observed population; both grains can omit the same entity. The River Bend example below demonstrates that limitation using independent NRC evidence.

The `daily_national_reconciliation` result supplies all 30 dates, published and child-summed values, both MW gaps, counts and null counts. `daily_facility_reconciliation` supplies every facility/date and its corresponding values. `mw_discrepancies` returns only nonzero or incomplete comparisons; it has **zero rows** for this window. There is no basis for inventing a capacity or outage MW mismatch.

## Published percentages: a measurable rounding difference

The published percentages are not additive measures. The valid aggregate is `100 * SUM(outage_mw) / SUM(capacity_mw)`. A capacity-weighted average of published child percentages is a separate diagnostic, because those percentages may already be rounded.

For **2026-09-03**, all three capacity sums are **97,620.5 MW** and all outage sums are **1,507.338 MW**, while the percentages differ:

| Percentage on 2026-09-03 | Value (%) | Difference from exact MW ratio (pp) |
| --- | ---: | ---: |
| Exact `100 × outage / capacity` | 1.544079368575248… | 0 |
| Published national | 1.54 | −0.004079368575248… |
| Capacity-weighted published facility percentages | 1.544451810838912… | +0.000372442263664… |
| Capacity-weighted published generator percentages | 1.544079368575248… | 0 |

This explanation is established by source arithmetic. Every one of the 714,657 generator observations has an integer reported percentage and satisfies the exact cross-product relation `capacity × reported_percentage = 100 × outage`. Across September, all 30 national rows and all 1,650 facility rows match the exact MW ratio rounded to two decimal places with `ROUND_HALF_UP`. The published ratio differs from the unrounded ratio on 30 national rows and 228 facility rows; **none fails that rounding rule**.

The SQL proves this using scaled integers before division. With `C = capacity × 10^9`, `O = outage × 10^9` and `P = percentage × 10^9`, all exact source values, the cross-product residual is `C×P − 100×O×10^9`. On September 3 the national residual is **−398.230 MW·pp**, and the facility-weighted numerator exceeds the generator-weighted numerator by **36.358 MW·pp**. These nonzero exact residuals establish that the small displayed differences are present in the published arithmetic. They are not binary floating-point noise. The integer HALF_UP test is implemented in `exact_percentage_rounding_summary`.

**Cause classification:** established rounding behavior in this snapshot. It does not establish how the upstream service implements every historical transformation. In particular, the cross-product identity with a zero capacity is degenerate and cannot validate a percentage; Indian Point below illustrates this.

**Product handling:** retain `reported_outage_pct` and the calculated metric separately. Label the displayed metric and show its numerator, denominator, units and source date. Recalculate aggregate percentages from MW sums, round only at presentation, and show compatible rounding differences as a diagnostic rather than an MW reconciliation failure. SQL division is useful for presentation; classification uses exact arithmetic. The model's published fleet metric uses Python `Decimal` with an explicit rounding contract.

Product-handling paragraphs below combine implemented preservation/review events with guidance for presenting entity-level findings. The current browser charts the national fleet; entity-specific charting and prescribed labels are presentation recommendations, not delivered entity charts.

## Indian Point 2: zero capacity with a nonzero reported percentage

**Observed entity:** Indian Point 2, facility `2497`, generator `2`. The source records zero capacity and zero outage for **91 consecutive days, 2020-01-01–2020-03-31**. Fourteen of those generator rows nevertheless have a nonzero reported outage percentage. The preceding observed day has positive capacity.

| Date | Capacity MW | Outage MW | Generator reported % | Facility reported % | Calculated % |
| --- | ---: | ---: | ---: | ---: | --- |
| 2019-12-31 | 1,025.9 | 0 | 0 | 0 | 0 |
| 2020-01-01 | 0 | 0 | 0 | 0 | Undefined |
| 2020-03-30 | 0 | 0 | 10 | 0 | Undefined |
| 2020-03-31 | 0 | 0 | 11 | 0 | Undefined |

Independent context matters: NRC reports Indian Point 2 at **90% power on March 30, 2020**, while EIA's retirement account places its retirement in **April 2020**. Therefore, the January–March capacity zeros must not be interpreted as proof that the physical reactor had already retired. The NRC percentage describes reported reactor status and does not establish electrical MW output or the missing capacity value. [NRC, March 30 status](https://www.nrc.gov/documents-reports/document-collections/events-reports-associated-with/power-reactor-status-reports/2020/20200330ps), [EIA retirement account](https://www.eia.gov/TodayinEnergy/detail.php?id=47776).

**Cause classification:** confirmed invalid denominator for a derived outage share; the upstream reason for the zero capacity is unknown. A zero numerator multiplied by that denominator can conceal disagreement between independently published percentages.

**Product handling:** flag `capacity_non_positive`, preserve both original percentages, and return null for a derived ratio whose denominator is non-positive. Display “unavailable: non-positive reported capacity,” with source values in the detail view. Do not convert `0/0` to 0%, remove the row, infer a shutdown date, or backfill 1,025.9 MW without an approved external source and an explicitly separate estimate. Queries: `zero_capacity_summary`, `indian_point_evidence`.

## River Bend: 120 missing days for an independently operating reactor

**Observed entity:** River Bend Station, facility `6462`, generator `1`. Both facility and generator routes have no observations for **2013-01-01–2013-04-30**, an inclusive interval of **120 calendar days**. The generator has 979 MW capacity and 0 outage on December 31, 2012, then reappears on May 1, 2013 at 969 MW capacity and 0 outage. No missing-date capacity or outage can be inferred from those boundary values.

NRC lists River Bend 1 at **100% power on January 15, 2013**, a date inside the missing interval. The omission is therefore inconsistent with interpreting every absent EIA entity as a retired or nonexistent unit. This is a source-coverage observation, not proof of a particular API defect. [NRC, January 15 status](https://www.nrc.gov/documents-reports/document-collections/events-reports-associated-with/power-reactor-status-reports/2013/20130115ps).

On that same date the published national capacity is **99,613.7 MW** and outage is **6,389.192 MW**. All observed facility and generator sums match those national measures exactly, despite both routes having **zero River Bend rows**. Reconciliation can pass when an operating entity is missing from every compared grain.

**Cause classification:** confirmed absence in the local source snapshot, with independent evidence of operation on a missing date. The origin of the omission and its effect on the intended EIA population remain unknown. A complete expected reactor roster requires an external, dated membership contract; observed first/last dates alone do not establish that contract.

**Product handling:** detect interior gaps per composite generator identity and per facility, record both boundary dates and gap length, and make the gap visible in coverage summaries and charts. Missing dates must remain gaps; do not draw them as 0% outage or silently forward-fill capacity. Preserve agreement diagnostics separately from coverage diagnostics. Queries: `river_bend_coverage_gap`, `river_bend_boundary_rows`, `river_bend_aggregate_blind_spot`.

## Grand Gulf: a 273 MW capacity step with unchanged reported outage percentage

**Observed entity:** Grand Gulf, facility `6072`, generator `1`. Its capacity falls from **1,463 MW on March 31, 2012 to 1,190 MW on April 1**, a **273 MW decrease (18.6603%)** between consecutive observations. Both rows report 100% outage, so published outage MW falls by the same 273 MW. The capacity returns to 1,463 MW on October 1, a **273 MW increase (22.9412%)**, with 0% reported outage on both sides of that boundary.

| Scope/date | Capacity MW | Outage MW | Published outage % |
| --- | ---: | ---: | ---: |
| Grand Gulf `6072/1`, 2012-03-31 | 1,463 | 1,463 | 100 |
| Grand Gulf `6072/1`, 2012-04-01 | 1,190 | 1,190 | 100 |
| Grand Gulf `6072/1`, 2012-09-30 | 1,190 | 0 | 0 |
| Grand Gulf `6072/1`, 2012-10-01 | 1,463 | 0 | 0 |

Both changes meet the implemented `capacity_change` review rule: an absolute change of at least **100 MW** and a relative change of at least **10%**, between consecutive calendar-day observations with a positive previous capacity. The rule raises a warning for review; it does not establish a physical cause. The exact source values and threshold check are returned by `grand_gulf_capacity_change`.

Additional fleet context supports investigating the capacity basis. Across the full history, **3,096 of 3,580 observed generator capacity changes occur on April 1 or October 1**. On October 1, 2026, the national denominator increases by **2,436.2 MW**, from 97,620.5 to 100,056.7 MW, with the exact same set of **95 generator identities**. Peach Bottom `3166/2` contributes an 82 MW increase (1,264.7 → 1,346.7), while its published percentage stays at 3% and outage increases from 37.941 to 40.401 MW. That smaller change is below the configured review threshold; the evidence query still exposes it. Same-day membership equality is tested with set differences, not inferred from equal counts.

**Cause classification:** confirmed source capacity change points; their upstream cause is not established. The calendar concentration is consistent with a seasonal reporting basis, since EIA documents distinct summer and winter generating capacities. That general definition does **not** prove this API's effective-date schedule or explain Grand Gulf's 18.66% decrease. An uprate, a correction and a seasonal capacity substitution must remain distinct possibilities until route-specific methodology or source evidence settles the case. [EIA capacity definitions](https://www.eia.gov/tools/faqs/faq.php?id=101&t=3).

**Product handling:** emit a `capacity_change` warning for changes meeting the configured threshold, and retain smaller changes in the daily facts. Expose before/after values and the rule that triggered review. Keep capacity as a daily fact rather than a fixed generator dimension attribute. Calculate outage share using the same date's denominator, and show absolute outage MW alongside the percentage so a denominator change remains explainable. A capacity change alone must not create a new generator identity or a physical restart event. Queries: `grand_gulf_capacity_change`, `fleet_capacity_change`, `fleet_identity_change`, `capacity_change_calendar`, `generator_capacity_change_evidence`.

## The model contains reproducible review events

All three primary anomalies have generated `warning` records in the pinned model's `data_quality_events` dataset. These are actual backend records, in addition to the independent source-fact queries above. The `automated_quality_event_evidence` query returns their event IDs and before/after measures.

| Event date | Generator identity | Generated rule | Recorded evidence |
| --- | --- | --- | --- |
| 2012-04-01 | Grand Gulf `6072/1` | `capacity_change` | Previous date March 31; capacity 1,463 → 1,190 MW |
| 2013-05-01 | River Bend `6462/1` | `entity_coverage_gap` | Previous date December 31, 2012; 120 missing days |
| 2020-03-31 | Indian Point 2 `2497/2` | `zero_capacity_positive_reported_pct` | Capacity/outage 0 MW; reported outage 11% |

The River Bend warning is attached to the first observed row after the gap; no fact rows are invented for the missing interval. These rules demonstrate reproducible detection, while the external evidence and cause qualifications remain necessary for interpretation.


### Review-event counts and overlap

The November 2 inspection observed the following rule counts in the model:

| Rule | Events |
| --- | ---: |
| `capacity_change` | 58 |
| `entity_coverage_gap` | 184 |
| `source_capacity_non_positive` | 182 |
| `zero_capacity` | 182 |
| `zero_capacity_positive_reported_pct` | 14 |
| **Total** | **620** |

There are 182 zero-capacity/source-nonpositive events because the 91-day anomaly is represented in related facility and generator observations. Multiple rules can apply to the same observation. **620 events does not mean 620 distinct bad source rows.** The precise rule identities, deterministic event-ID inputs and thresholds are in the [model guide](DATA_PIPELINE_AND_MODEL.md#comparing-grains-and-detecting-review-events).

## Interpretation limits and follow-up

The user-facing fleet metric is the fraction of **published national capacity reported offline that day**: `outage_mw / capacity_mw`. Its percentage representation multiplies that ratio by 100. Capacity and instantaneous reported status do not measure energy produced over a day; the metric should not be labeled electricity generation, daily MWh loss or capacity factor. [EIA distinguishes capacity from generation](https://www.eia.gov/tools/faqs/faq.php?id=101&t=3).

The implementation should preserve the source and annotate these observations. It should not “repair” historical values merely because they are unusual. The highest-value upstream questions are: what is the dated eligible reactor population; why are River Bend's 120 days absent from both routes; why does Indian Point 2 have zero January–March capacity; and what capacity basis/effective-date rules explain April/October changes? Their answers can refine quality rules and future model versions without rewriting the evidence recorded here.
