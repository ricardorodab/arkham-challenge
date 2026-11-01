"""Pure, versioned analytical transformation of a pinned source snapshot.

This module neither reads credentials nor selects/publishes a current version.
Its caller owns snapshot selection, the writer lock and the catalog transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Any

import duckdb

from .domain import ROUTES, DataValidationError, canonical_json, identifier
from .model_contracts import DATASETS, METRICS, MODEL_VERSION, MW_TOLERANCE, PCT_TOLERANCE


class ModelError(DataValidationError):
    code = "invalid_model"


ModelBuildError = ModelError


@dataclass(frozen=True)
class ModelBundle:
    id: str
    path: Path
    manifest: dict[str, Any]


def _normalized(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def logical_digest(
    connection: duckdb.DuckDBPyConnection, table_or_query: str, primary_key: tuple[str, ...]
) -> str:
    """Hash ordered typed rows; input SQL/identifiers must be application-owned."""
    query = table_or_query.strip()
    if not query.lower().startswith(("select ", "with ")):
        query = f"SELECT * FROM {identifier(query)}"
    ordering = ",".join(identifier(name) for name in primary_key)
    cursor = connection.execute(f"SELECT * FROM ({query}) AS digest_input ORDER BY {ordering}")
    digest = hashlib.sha256()
    while rows := cursor.fetchmany(8192):
        for row in rows:
            digest.update(canonical_json([_normalized(value) for value in row]).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _create(con: duckdb.DuckDBPyConnection, dataset: str, query: str | None = None) -> None:
    spec = DATASETS[dataset]
    columns = ",".join(f"{identifier(name)} {kind}" for name, kind in spec.columns)
    con.execute(f"CREATE TABLE {identifier(dataset)} ({columns})")
    if query:
        con.execute(f"INSERT INTO {identifier(dataset)} {query}")


def _insert_rows(
    con: duckdb.DuckDBPyConnection, dataset: str, rows: Iterable[tuple[Any, ...]], destination: Path
) -> None:
    spec = DATASETS[dataset]
    source = destination / f"_{dataset}.jsonl"
    try:
        count = 0
        with source.open("w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                value = {
                    name: _normalized(item)
                    for (name, _), item in zip(spec.columns, row, strict=True)
                }
                stream.write(canonical_json(value) + "\n")
                count += 1
        if not count:
            return
        schema = "{" + ",".join(f"'{name}':'{kind}'" for name, kind in spec.columns) + "}"
        con.execute(
            f"INSERT INTO {identifier(dataset)} SELECT * FROM read_json(?, "
            f"columns={schema}, format='newline_delimited')",
            [str(source)],
        )
    finally:
        source.unlink(missing_ok=True)


def _source_contract(
    con: duckdb.DuckDBPyConnection, source_paths: dict[str, Path]
) -> dict[str, Any]:
    if set(source_paths) != {spec.name for spec in ROUTES}:
        raise ModelError("The model requires exactly the three source grains.")
    result = {}
    for spec in ROUTES:
        source = Path(source_paths[spec.name]).resolve()
        if not source.is_file():
            raise ModelError("A required source file is unavailable.")
        table = f"_raw_{spec.name}"
        # Paths originate from the pinned server-owned source descriptor. SQL
        # string quoting also handles apostrophes in legitimate directory names.
        # Avoid mixing DuckDB's relation API with an unexhausted scalar result.
        literal = str(source).replace("'", "''")
        con.execute(f"CREATE VIEW {table} AS SELECT * FROM read_parquet('{literal}')")
        schema = [
            (item[0], item[1]) for item in con.execute(f"DESCRIBE SELECT * FROM {table}").fetchall()
        ]
        if schema != list(spec.columns):
            raise ModelError(f"{spec.name}: source schema requires an explicit migration.")
        keys = ",".join(identifier(name) for name in spec.key_fields)
        nulls = " OR ".join(f"{identifier(name)} IS NULL" for name in spec.key_fields)
        count, distinct, missing = con.execute(
            f"SELECT count(*),count(DISTINCT ({keys})),count(*) FILTER (WHERE {nulls}) FROM {table}"
        ).fetchone()
        if not count or count != distinct or missing:
            raise ModelError(f"{spec.name}: source keys must be nonempty, unique and non-null.")
        wrong_units = con.execute(
            f"SELECT count(*) FROM {table} WHERE \"capacity-units\" IS DISTINCT FROM 'megawatts' "
            "OR \"outage-units\" IS DISTINCT FROM 'megawatts' "
            "OR \"percentOutage-units\" IS DISTINCT FROM 'percent'"
        ).fetchone()[0]
        if wrong_units:
            raise ModelError(f"{spec.name}: source units are incompatible.")
        result[spec.name] = {"sha256": _sha256(source), "rows": count}
    return result


def _facts_and_dimensions(con: duckdb.DuckDBPyConnection) -> None:
    suffix = 'capacity,outage,"percentOutage",_quality_flags,_row_hash'
    _create(con, "national_daily", f"SELECT period,{suffix} FROM _raw_national")
    _create(
        con, "facility_daily", f'SELECT period,facility,"facilityName",{suffix} FROM _raw_facility'
    )
    _create(
        con,
        "generator_daily",
        f'SELECT period,facility,generator,"facilityName",{suffix} FROM _raw_generator',
    )
    _create(
        con,
        "dim_date",
        """
        SELECT period,year(period)::INTEGER,month(period)::INTEGER,day(period)::INTEGER
        FROM (SELECT period FROM national_daily UNION SELECT period FROM facility_daily
              UNION SELECT period FROM generator_daily)
    """,
    )
    _create(
        con,
        "dim_facility",
        """
        WITH identities AS (
            SELECT period,facility_id,facility_name,0 AS priority,true AS f,false AS g FROM facility_daily
            UNION ALL SELECT period,facility_id,facility_name,1,false,true FROM generator_daily
        ), bounds AS (
            SELECT facility_id,min(period) AS first_observed,max(period) AS last_observed,
                   bool_or(f) AS seen_in_facility_route,bool_or(g) AS seen_in_generator_route
            FROM identities GROUP BY facility_id
        ), names AS (
            SELECT facility_id,facility_name FROM identities WHERE facility_name IS NOT NULL
            QUALIFY row_number() OVER(PARTITION BY facility_id
                                     ORDER BY period DESC,priority,facility_name)=1
        )
        SELECT b.facility_id,n.facility_name,b.first_observed,b.last_observed,
               b.seen_in_facility_route,b.seen_in_generator_route
        FROM bounds b LEFT JOIN names n USING(facility_id)
    """,
    )
    _create(
        con,
        "dim_generator",
        """
        SELECT facility_id,generator_id,min(period),max(period)
        FROM generator_daily GROUP BY facility_id,generator_id
    """,
    )


def _pct(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_UP
        return (numerator / denominator).quantize(Decimal("0.000000001"))


def _gap(child: Decimal | None, parent: Decimal | None) -> Decimal | None:
    if child is None or parent is None:
        return None
    # Aggregate MW uses DECIMAL(38,9), wider than Python's default 28 digits.
    # Subtraction must not silently round a gap before it reaches Parquet.
    with localcontext() as context:
        context.prec = 50
        return child - parent


def _comparison(gaps: list[Decimal | None], tolerance: str, *, pct: bool = False) -> str:
    if any(item is None for item in gaps):
        return "undefined"
    if all(item == 0 for item in gaps):
        return "exact"
    if all(item.copy_abs() <= Decimal(tolerance) for item in gaps):
        return "within_rounding_tolerance" if pct else "within_tolerance"
    return "mismatch"


def _fleet(con: duckdb.DuckDBPyConnection, destination: Path) -> None:
    _create(con, "fleet_daily")
    rows = con.execute(
        "SELECT period,capacity_mw,outage_mw,reported_outage_pct,quality_flags "
        "FROM national_daily ORDER BY period"
    ).fetchall()
    output = []
    for period, capacity, outage, reported, raw_flags in rows:
        flags = json.loads(raw_flags)
        if not isinstance(flags, list):
            raise ModelError("Source quality flags must be JSON arrays.")
        share = pct = None
        if capacity is None or capacity <= 0 or outage is None:
            flags = sorted(set(flags + ["metric_undefined"]))
        else:
            with localcontext() as context:
                context.prec = 28
                context.rounding = ROUND_HALF_UP
                ratio = outage / capacity
                share = ratio.quantize(Decimal("0.000000000001"))
                pct = (ratio * 100).quantize(Decimal("0.000000001"))
        output.append(
            (
                period,
                capacity,
                outage,
                reported,
                share,
                pct,
                _gap(reported, pct),
                canonical_json(flags),
            )
        )
    _insert_rows(con, "fleet_daily", output, destination)


def _aggregates(con: duckdb.DuckDBPyConnection) -> None:
    # Nulls cannot disappear into SUM: only a fully observed measure has a total.
    for dataset, grouping in (
        ("generator_daily", "period,facility_id"),
        ("generator_daily", "period"),
        ("facility_daily", "period"),
    ):
        name = (
            "_generator_facility" if "," in grouping else "_" + dataset.split("_")[0] + "_national"
        )
        con.execute(f"""CREATE TABLE {name} AS
            SELECT {grouping},count(*)::INTEGER AS entity_count,
                   (count(*)-count(capacity_mw))::INTEGER AS null_capacity,
                   (count(*)-count(outage_mw))::INTEGER AS null_outage,
                   (count(*)-count(reported_outage_pct))::INTEGER AS null_pct,
                   CASE WHEN count(*)=count(capacity_mw) THEN sum(capacity_mw) END AS capacity,
                   CASE WHEN count(*)=count(outage_mw) THEN sum(outage_mw) END AS outage,
                   CASE WHEN count(*)=count(capacity_mw) AND count(*)=count(reported_outage_pct)
                        THEN sum(capacity_mw*reported_outage_pct) END AS weighted_numerator
            FROM {dataset} GROUP BY {grouping}
        """)


def _reconciliation(con: duckdb.DuckDBPyConnection, destination: Path) -> None:
    _aggregates(con)
    _create(con, "facility_reconciliation_daily")
    query = """
        SELECT coalesce(f.period,g.period),coalesce(f.facility_id,g.facility_id),
               coalesce(f.facility_name,d.facility_name),f.period IS NOT NULL,
               coalesce(g.entity_count,0),coalesce(g.null_capacity,0),coalesce(g.null_outage,0),
               coalesce(g.null_pct,0),f.capacity_mw,g.capacity,f.outage_mw,g.outage,
               f.reported_outage_pct,g.weighted_numerator
        FROM facility_daily f FULL OUTER JOIN _generator_facility g
        ON f.period=g.period AND f.facility_id=g.facility_id
        LEFT JOIN dim_facility d ON d.facility_id=coalesce(f.facility_id,g.facility_id)
        ORDER BY 1,2
    """

    def facility_rows():
        reader = con.execute(query)
        while batch := reader.fetchmany(8192):
            for period, facility, name, present, count, nc, no, np, fc, gc, fo, go, fp, wn in batch:
                cg, og = _gap(gc, fc), _gap(go, fo)
                gp = _pct(wn, gc)
                pg = _gap(gp, fp)
                coverage = (
                    "missing_parent"
                    if not present
                    else "missing_children"
                    if not count
                    else (
                        "null_measure"
                        if nc or no or np or fc is None or fo is None or fp is None
                        else "both_observed"
                    )
                )
                yield (
                    period,
                    facility,
                    name,
                    present,
                    count,
                    nc,
                    no,
                    np,
                    fc,
                    gc,
                    fo,
                    go,
                    cg,
                    og,
                    cg.copy_abs() if cg is not None else None,
                    og.copy_abs() if og is not None else None,
                    fp,
                    gp,
                    pg,
                    coverage,
                    _comparison([cg, og], MW_TOLERANCE),
                    _comparison([pg], PCT_TOLERANCE, pct=True),
                )

    _insert_rows(con, "facility_reconciliation_daily", facility_rows(), destination)
    _create(con, "national_reconciliation_daily")
    query = """
        WITH days AS (SELECT period FROM national_daily UNION SELECT period FROM facility_daily
                      UNION SELECT period FROM generator_daily)
        SELECT d.period,n.period IS NOT NULL,coalesce(f.entity_count,0),coalesce(g.entity_count,0),
               coalesce(f.null_capacity,0),coalesce(f.null_outage,0),coalesce(f.null_pct,0),
               coalesce(g.null_capacity,0),coalesce(g.null_outage,0),coalesce(g.null_pct,0),
               n.capacity_mw,f.capacity,g.capacity,n.outage_mw,f.outage,g.outage,
               n.reported_outage_pct,f.weighted_numerator,g.weighted_numerator
        FROM days d LEFT JOIN national_daily n USING(period)
        LEFT JOIN _facility_national f USING(period) LEFT JOIN _generator_national g USING(period)
        ORDER BY d.period
    """
    output = []
    for (
        period,
        present,
        fc,
        gc,
        fnc,
        fno,
        fnp,
        gnc,
        gno,
        gnp,
        nc,
        fcap,
        gcap,
        no,
        fout,
        gout,
        npct,
        fwn,
        gwn,
    ) in con.execute(query).fetchall():
        gaps = [_gap(fcap, nc), _gap(gcap, nc), _gap(fout, no), _gap(gout, no)]
        fp, gp = _pct(fwn, fcap), _pct(gwn, gcap)
        pgaps = [_gap(fp, npct), _gap(gp, npct)]
        coverage = (
            "missing_parent"
            if not present
            else "missing_children"
            if not fc or not gc
            else (
                "null_measure"
                if any((fnc, fno, fnp, gnc, gno, gnp))
                or any(item is None for item in (nc, no, npct))
                else "both_observed"
            )
        )
        output.append(
            (
                period,
                present,
                fc,
                gc,
                fnc,
                fno,
                fnp,
                gnc,
                gno,
                gnp,
                nc,
                fcap,
                gcap,
                no,
                fout,
                gout,
                *gaps,
                *(x.copy_abs() if x is not None else None for x in gaps),
                npct,
                fp,
                gp,
                *pgaps,
                coverage,
                _comparison(gaps, MW_TOLERANCE),
                _comparison(pgaps, PCT_TOLERANCE, pct=True),
            )
        )
    _insert_rows(con, "national_reconciliation_daily", output, destination)


def _events(con: duckdb.DuckDBPyConnection, destination: Path) -> None:
    _create(con, "data_quality_events")
    events: dict[str, tuple[Any, ...]] = {}

    def add(
        rule,
        period,
        grain,
        facility,
        generator,
        *,
        previous=None,
        gap=None,
        before=None,
        after=None,
        outage=None,
        pct=None,
        details=None,
    ):
        event_id = hashlib.sha256(
            canonical_json(
                [rule, str(period), grain, facility, generator, str(previous) if previous else None]
            ).encode("utf-8")
        ).hexdigest()
        events[event_id] = (
            event_id,
            period,
            grain,
            facility,
            generator,
            rule,
            "warning",
            previous,
            gap,
            before,
            after,
            outage,
            pct,
            canonical_json(
                details
                or {"interpretation": "Review source observations; no physical cause is asserted."}
            ),
        )

    for grain, dataset, entities in (
        ("national", "national_daily", ()),
        ("facility", "facility_daily", ("facility_id",)),
        ("generator", "generator_daily", ("facility_id", "generator_id")),
    ):
        facility = "facility_id" if entities else "NULL::VARCHAR"
        generator = "generator_id" if len(entities) == 2 else "NULL::VARCHAR"
        window = (
            "PARTITION BY " + ",".join(entities) + " " if entities else ""
        ) + "ORDER BY period"
        query = f"""SELECT period,{facility},{generator},capacity_mw,outage_mw,reported_outage_pct,
                    quality_flags,lag(period) OVER({window}),lag(capacity_mw) OVER({window})
                    FROM {dataset} ORDER BY {",".join((*entities, "period"))}"""
        cursor = con.execute(query)
        while batch := cursor.fetchmany(8192):
            for period, fac, gen, cap, outage, pct, flags, previous, before in batch:
                args = {"after": cap, "outage": outage, "pct": pct}
                if cap == 0:
                    add("zero_capacity", period, grain, fac, gen, **args)
                    if pct is not None and pct > 0:
                        add("zero_capacity_positive_reported_pct", period, grain, fac, gen, **args)
                if flags != "[]":
                    parsed = json.loads(flags)
                    if not isinstance(parsed, list) or any(
                        not isinstance(flag, str) for flag in parsed
                    ):
                        raise ModelError("Source quality flags must be JSON arrays of strings.")
                    for flag in parsed:
                        add("source_" + flag, period, grain, fac, gen, **args)
                if previous:
                    gap = (period - previous).days
                    if gap > 1:
                        add(
                            "entity_coverage_gap",
                            period,
                            grain,
                            fac,
                            gen,
                            previous=previous,
                            gap=gap - 1,
                            before=before,
                            **args,
                        )
                    if gap == 1 and before is not None and before > 0 and cap is not None:
                        with localcontext() as context:
                            context.prec = 50
                            change = abs(cap - before)
                            if change >= 100 and change / before >= Decimal("0.10"):
                                add(
                                    "capacity_change",
                                    period,
                                    grain,
                                    fac,
                                    gen,
                                    previous=previous,
                                    before=before,
                                    **args,
                                )
    for period, facility, coverage in con.execute(
        "SELECT period,facility_id,coverage_status FROM facility_reconciliation_daily "
        "WHERE coverage_status IN ('missing_parent','missing_children')"
    ).fetchall():
        add(coverage, period, "facility", facility, None)
    _insert_rows(con, "data_quality_events", list(events.values()), destination)


def validate_model_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Independent primary/foreign-key checks before any output is published."""
    for dataset, spec in DATASETS.items():
        keys = ",".join(identifier(name) for name in spec.primary_key)
        nulls = " OR ".join(f"{identifier(name)} IS NULL" for name in spec.primary_key)
        count, distinct, missing = con.execute(
            f"SELECT count(*),count(DISTINCT ({keys})),count(*) FILTER(WHERE {nulls}) "
            f"FROM {identifier(dataset)}"
        ).fetchone()
        if count != distinct or missing:
            raise ModelError(f"{dataset}: invalid primary keys.")
        for relation in spec.foreign_keys:
            conditions = " AND ".join(
                f"c.{identifier(a)}=p.{identifier(b)}"
                for a, b in zip(relation["columns"], relation["references"], strict=True)
            )
            all_not_null = " AND ".join(
                f"c.{identifier(a)} IS NOT NULL" for a in relation["columns"]
            )
            absent = con.execute(
                f"SELECT count(*) FROM {identifier(dataset)} c "
                f"WHERE {all_not_null} AND NOT EXISTS (SELECT 1 FROM "
                f"{identifier(relation['dataset'])} p WHERE {conditions})"
            ).fetchone()[0]
            if absent:
                raise ModelError(f"{dataset}: invalid foreign keys.")


def build_model(
    source_paths: dict[str, Path], source_snapshot_id: str, destination: Path, *, model_id: str
) -> ModelBundle:
    """Build into a fresh directory; caller publishes the verified immutable bundle."""
    if any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None
        for value in (source_snapshot_id, model_id)
    ):
        raise ModelError("Model and source identifiers must be UUID hex strings.")
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ModelError("Model destination must be fresh and empty.")
    destination.mkdir(parents=True, exist_ok=True)
    database = destination / "_model.duckdb"
    con = duckdb.connect(str(database), config={"threads": 1, "memory_limit": "256MB"})
    try:
        source_files = _source_contract(con, source_paths)
        _facts_and_dimensions(con)
        _fleet(con, destination)
        _reconciliation(con, destination)
        _events(con, destination)
        validate_model_tables(con)
        outputs = {}
        for dataset, spec in DATASETS.items():
            ordering = ",".join(identifier(name) for name in spec.primary_key)
            path = destination / f"{dataset}.parquet"
            con.execute(
                f"COPY (SELECT * FROM {identifier(dataset)} ORDER BY {ordering}) "
                "TO ? (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)",
                [str(path)],
            )
            with path.open("r+b") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            count = con.execute(f"SELECT count(*) FROM {identifier(dataset)}").fetchone()[0]
            minimum = maximum = None
            if any(name == "period" for name, _ in spec.columns):
                minimum, maximum = con.execute(
                    f"SELECT min(period),max(period) FROM {identifier(dataset)}"
                ).fetchone()
            outputs[dataset] = {
                **spec.catalog_definition(),
                "file": path.name,
                "rows": count,
                "sha256": _sha256(path),
                "logical_sha256": logical_digest(con, dataset, spec.primary_key),
                "min_period": minimum.isoformat() if minimum else None,
                "max_period": maximum.isoformat() if maximum else None,
            }
        rules = dict(
            con.execute(
                "SELECT rule_id,count(*) FROM data_quality_events GROUP BY rule_id ORDER BY rule_id"
            ).fetchall()
        )
        manifest = {
            "model_version": MODEL_VERSION,
            "model_id": model_id,
            "source_snapshot_id": source_snapshot_id,
            "created_at": datetime.now(UTC).isoformat(),
            "source_files": source_files,
            "datasets": outputs,
            "metrics": METRICS,
            "quality": {
                "event_counts": rules,
                "mw_tolerance": MW_TOLERANCE,
                "reported_pct_tolerance_pp": PCT_TOLERANCE,
                "coverage": "Observed records only; historical expected entity inventory is unknown.",
            },
            "partitioning": {
                "strategy": "one sorted ZSTD file per dataset",
                "row_group_size": 122880,
                "reason": "Current source files are below 30 MB; annual/entity partitions would be tiny.",
            },
        }
        with (destination / "manifest.json").open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(manifest) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            handle = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
        return ModelBundle(model_id, destination, manifest)
    except ModelError:
        raise
    except (duckdb.Error, ValueError, TypeError, OSError, KeyError, ArithmeticError):
        raise ModelError("Analytical model validation or persistence failed.") from None
    finally:
        con.close()
        with suppress(OSError):
            database.unlink(missing_ok=True)
            database.with_suffix(".duckdb.wal").unlink(missing_ok=True)
            temporary = database.with_suffix(".duckdb.tmp")
            if temporary.is_dir():
                temporary.rmdir()
