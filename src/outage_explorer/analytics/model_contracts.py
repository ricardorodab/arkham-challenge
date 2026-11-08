"""Declare the analytical schema, keys, access metadata and metric semantics.

Model version 2 corrects absolute-percent versus percentage-point catalog labels;
the fleet metric remains version 1. Historical model-v1 semantics are loaded from
a frozen packaged profile rather than rewriting immutable manifests. Definitions
are independent of storage and HTTP identity enforcement.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

MODEL_VERSION = 2
MW = "DECIMAL(24,9)"
TOTAL_MW = "DECIMAL(38,9)"
PCT = "DECIMAL(24,9)"
SHARE = "DECIMAL(24,12)"
MW_TOLERANCE = "0.01"
PCT_TOLERANCE = "0.005"


@dataclass(frozen=True)
class DatasetSpec:
    """Describe one dataset's ordered physical and semantic model contract.

    Primary/foreign keys are validated explicitly because Parquet does not enforce
    relationships. Classification defaults to detail; national classification allows
    Viewer metadata/access in the trusted adapter, while HTTP authentication remains
    separate. The frozen dataclass does not make nested mapping values deeply immutable.
    """

    id: str
    columns: tuple[tuple[str, str], ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[dict[str, Any], ...] = ()
    classification: str = "detail"
    description: str = ""

    def catalog_definition(self) -> dict[str, Any]:
        """Return detached JSON-compatible schema, keys, units and persona metadata.

        Derive units from declared measure names, distinguishing percentages from their
        differences. Copy mutable relationship definitions so consumers cannot mutate the
        shared contract through this returned metadata.
        """
        return {
            "id": self.id,
            "schema": [list(item) for item in self.columns],
            "primary_key": list(self.primary_key),
            "foreign_keys": deepcopy(list(self.foreign_keys)),
            "classification": self.classification,
            "description": self.description,
            "allowed_personas": ["Viewer", "Analyst", "Admin"]
            if self.classification == "national"
            else ["Analyst", "Admin"],
            "units": {
                name: "MW"
                if name.endswith("_mw")
                else "percentage points"
                if name.endswith("_pp") or name == "reported_vs_calculated_pct_gap"
                else "percent"
                if name.endswith("_pct")
                else "fraction"
                for name, _ in self.columns
                if name.endswith(("_mw", "_pct", "_pp"))
                or name in {"offline_share", "reported_vs_calculated_pct_gap"}
            },
        }


def fk(columns: tuple[str, ...], dataset: str, references: tuple[str, ...]) -> dict[str, Any]:
    """Construct a JSON-compatible ordered foreign-key relationship descriptor.

    Callers supply matching column/reference tuples from the trusted schema; this
    helper serializes declarations rather than checking referential integrity.
    """
    return {"columns": list(columns), "dataset": dataset, "references": list(references)}


DATE_FK = fk(("period",), "dim_date", ("period",))
FACILITY_FK = fk(("facility_id",), "dim_facility", ("facility_id",))
GENERATOR_FK = fk(("facility_id", "generator_id"), "dim_generator", ("facility_id", "generator_id"))
MEASURES = (
    ("capacity_mw", MW),
    ("outage_mw", MW),
    ("reported_outage_pct", PCT),
    ("quality_flags", "VARCHAR"),
    ("source_row_hash", "VARCHAR"),
)

DATASETS: dict[str, DatasetSpec] = {}


def _add(spec: DatasetSpec) -> None:
    """Register a trusted dataset specification by ID during module initialization."""
    DATASETS[spec.id] = spec


_add(
    DatasetSpec(
        "dim_date",
        (("period", "DATE"), ("year", "INTEGER"), ("month", "INTEGER"), ("day", "INTEGER")),
        ("period",),
        description="Union of observed source dates.",
    )
)
_add(
    DatasetSpec(
        "dim_facility",
        (
            ("facility_id", "VARCHAR"),
            ("facility_name", "VARCHAR"),
            ("first_observed", "DATE"),
            ("last_observed", "DATE"),
            ("seen_in_facility_route", "BOOLEAN"),
            ("seen_in_generator_route", "BOOLEAN"),
        ),
        ("facility_id",),
        description="Union of source identities; latest observed descriptive name.",
    )
)
_add(
    DatasetSpec(
        "dim_generator",
        (
            ("facility_id", "VARCHAR"),
            ("generator_id", "VARCHAR"),
            ("first_observed", "DATE"),
            ("last_observed", "DATE"),
        ),
        ("facility_id", "generator_id"),
        (FACILITY_FK,),
        description="Generator identity is scoped to its facility.",
    )
)
_add(
    DatasetSpec(
        "national_daily",
        (("period", "DATE"), *MEASURES),
        ("period",),
        (DATE_FK,),
        "national",
        "Published national observations, without cross-grain substitutions.",
    )
)
_add(
    DatasetSpec(
        "facility_daily",
        (("period", "DATE"), ("facility_id", "VARCHAR"), ("facility_name", "VARCHAR"), *MEASURES),
        ("period", "facility_id"),
        (DATE_FK, FACILITY_FK),
        description="Facility observations with observation-time names.",
    )
)
_add(
    DatasetSpec(
        "generator_daily",
        (
            ("period", "DATE"),
            ("facility_id", "VARCHAR"),
            ("generator_id", "VARCHAR"),
            ("facility_name", "VARCHAR"),
            *MEASURES,
        ),
        ("period", "facility_id", "generator_id"),
        (DATE_FK, GENERATOR_FK),
        description="Preserved generator observations, including missing facility-day parents.",
    )
)
_add(
    DatasetSpec(
        "fleet_daily",
        (
            ("period", "DATE"),
            ("capacity_mw", MW),
            ("outage_mw", MW),
            ("reported_outage_pct", PCT),
            ("offline_share", SHARE),
            ("offline_pct", PCT),
            ("reported_vs_calculated_pct_gap", PCT),
            ("quality_flags", "VARCHAR"),
        ),
        ("period",),
        (DATE_FK, fk(("period",), "national_daily", ("period",))),
        "national",
        "US-08: share of published national fleet capacity offline each day.",
    )
)

FACILITY_RECON_COLUMNS = (
    ("period", "DATE"),
    ("facility_id", "VARCHAR"),
    ("facility_name", "VARCHAR"),
    ("facility_present", "BOOLEAN"),
    ("generator_count", "INTEGER"),
    ("generator_null_capacity_count", "INTEGER"),
    ("generator_null_outage_count", "INTEGER"),
    ("generator_null_pct_count", "INTEGER"),
    ("facility_capacity_mw", MW),
    ("generator_capacity_mw", TOTAL_MW),
    ("facility_outage_mw", MW),
    ("generator_outage_mw", TOTAL_MW),
    ("capacity_gap_mw", TOTAL_MW),
    ("outage_gap_mw", TOTAL_MW),
    ("capacity_abs_gap_mw", TOTAL_MW),
    ("outage_abs_gap_mw", TOTAL_MW),
    ("facility_reported_pct", PCT),
    ("generator_weighted_reported_pct", PCT),
    ("reported_pct_gap_pp", PCT),
    ("coverage_status", "VARCHAR"),
    ("mw_comparison_status", "VARCHAR"),
    ("pct_comparison_status", "VARCHAR"),
)
_add(
    DatasetSpec(
        "facility_reconciliation_daily",
        FACILITY_RECON_COLUMNS,
        ("period", "facility_id"),
        (DATE_FK, FACILITY_FK),
        description="Observed generator sums versus published facility; absent/null is not zero.",
    )
)

NATIONAL_RECON_COLUMNS = (
    ("period", "DATE"),
    ("national_present", "BOOLEAN"),
    ("facility_count", "INTEGER"),
    ("generator_count", "INTEGER"),
    ("facility_null_capacity_count", "INTEGER"),
    ("facility_null_outage_count", "INTEGER"),
    ("facility_null_pct_count", "INTEGER"),
    ("generator_null_capacity_count", "INTEGER"),
    ("generator_null_outage_count", "INTEGER"),
    ("generator_null_pct_count", "INTEGER"),
    ("national_capacity_mw", MW),
    ("facility_capacity_mw", TOTAL_MW),
    ("generator_capacity_mw", TOTAL_MW),
    ("national_outage_mw", MW),
    ("facility_outage_mw", TOTAL_MW),
    ("generator_outage_mw", TOTAL_MW),
    ("facility_capacity_gap_mw", TOTAL_MW),
    ("generator_capacity_gap_mw", TOTAL_MW),
    ("facility_outage_gap_mw", TOTAL_MW),
    ("generator_outage_gap_mw", TOTAL_MW),
    ("facility_capacity_abs_gap_mw", TOTAL_MW),
    ("generator_capacity_abs_gap_mw", TOTAL_MW),
    ("facility_outage_abs_gap_mw", TOTAL_MW),
    ("generator_outage_abs_gap_mw", TOTAL_MW),
    ("national_reported_pct", PCT),
    ("facility_weighted_reported_pct", PCT),
    ("generator_weighted_reported_pct", PCT),
    ("facility_reported_pct_gap_pp", PCT),
    ("generator_reported_pct_gap_pp", PCT),
    ("coverage_status", "VARCHAR"),
    ("mw_comparison_status", "VARCHAR"),
    ("pct_comparison_status", "VARCHAR"),
)
_add(
    DatasetSpec(
        "national_reconciliation_daily",
        NATIONAL_RECON_COLUMNS,
        ("period",),
        (DATE_FK,),
        description="Published national values versus separately aggregated facilities/generators.",
    )
)

EVENT_COLUMNS = (
    ("event_id", "VARCHAR"),
    ("period", "DATE"),
    ("grain", "VARCHAR"),
    ("facility_id", "VARCHAR"),
    ("generator_id", "VARCHAR"),
    ("rule_id", "VARCHAR"),
    ("severity", "VARCHAR"),
    ("previous_period", "DATE"),
    ("gap_days", "INTEGER"),
    ("before_capacity_mw", MW),
    ("after_capacity_mw", MW),
    ("outage_mw", MW),
    ("reported_outage_pct", PCT),
    ("details", "VARCHAR"),
)
_add(
    DatasetSpec(
        "data_quality_events",
        EVENT_COLUMNS,
        ("event_id",),
        (DATE_FK,),
        description="Reviewable source anomalies; flags do not establish physical causes.",
    )
)

METRICS = {
    "fleet_offline_share": {
        "version": 1,
        "dataset": "fleet_daily",
        "column": "offline_share",
        "unit": "fraction",
        "formula": "published national outage_mw / published national capacity_mw",
        "source_dataset": "national_daily",
        "source_grain": "national per calendar day",
        "denominator_policy": "Null/non-positive capacity or null outage yields null plus a flag.",
        "rounding": "Python Decimal precision 28, ROUND_HALF_UP; share 12 and percentage 9 decimal places.",
        "outlier_policy": "No clipping: finite out-of-range source values remain visible with flags.",
        "percentage_column": "offline_pct",
        "definition": "offline_pct = 100 * the unrounded outage/capacity ratio; share and percent are rounded independently",
    }
}


def contract_profile(version: int) -> dict[str, Any]:
    """Return the current semantic profile or the frozen historical v1 profile.

    Version 2 fixes catalog unit labels without changing numeric observations or
    metric version 1. Load v1 from the packaged JSON, including its warning, so old
    artifacts retain original semantics. Reject unsupported/non-integer versions
    including booleans with ``ValueError``. Returned mutable metadata is detached
    from current shared definitions.
    """
    if type(version) is not int:
        raise ValueError("Unsupported analytical model version.")
    if version == MODEL_VERSION:
        return {
            "model_version": MODEL_VERSION,
            "datasets": {key: value.catalog_definition() for key, value in DATASETS.items()},
            "metrics": deepcopy(METRICS),
        }
    if version == 1:
        return json.loads(
            files("outage_explorer.analytics")
            .joinpath("contracts/model-v1.json")
            .read_text(encoding="utf-8")
        )
    raise ValueError("Unsupported analytical model version.")
