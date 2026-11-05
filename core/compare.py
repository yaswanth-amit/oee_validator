"""Pair records from both sides and compare every mapped column exactly (Polars).

Pairing key: machine_name (from machines.csv) + the table's other key columns
(datatype, st_time) + an occurrence number when the same key repeats on a side
(ordered by source id). Every paired record is compared column by column on its
canonical text; null equals null.

Each record ends up as one of: identical, different, only_sqlserver, only_postgres.
A one-sided record that matches a `known_differences` rule carries its reason and does
not fail the verdict - it is an expected gap, listed separately.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from .config import Aliases, TableSpec
from .db import INFO_PREFIX, SOURCE_ID
from .normalize import NULL_KEY, canonical_series, pairing_key

SIDES = ("sqlserver", "postgres")
SUFFIX = {"sqlserver": "sql", "postgres": "pg"}
OCC = "occurrence"

IDENTICAL = "identical"
DIFFERENT = "different"
ONLY_SQL = "only_sqlserver"
ONLY_PG = "only_postgres"
RESULTS = (IDENTICAL, DIFFERENT, ONLY_SQL, ONLY_PG)

PASS = "PASS"
PASS_KNOWN = "PASS (known gaps only)"
FAIL = "FAIL"
NO_DATA = "NO DATA"


# --------------------------------------------------------------------------- one side

def _source_ids(values: list) -> pl.Series:
    try:
        return pl.Series(SOURCE_ID, values, dtype=pl.Int64, strict=True)
    except (TypeError, ValueError, OverflowError, pl.exceptions.PolarsError):
        return pl.Series(SOURCE_ID, [None if v is None else str(v) for v in values], dtype=pl.String)


def _machine_lookup(values: pl.Series, id_to_name: dict[str, str]) -> pl.Series:
    """Record machine id -> machine_name. Exact (trimmed) match first, then ignoring leading zeros."""
    def norm(s: str) -> str:
        t = s.strip().lstrip("0")
        return t or ("0" if s.strip() else "")

    loose = {norm(k): v for k, v in id_to_name.items()}
    out = []
    for v in values.to_list():
        if v is None:
            out.append("<no machine>")
            continue
        s = v.strip()
        out.append(id_to_name.get(s) or loose.get(norm(s)) or f"<unlisted machine {s}>")
    return pl.Series("machine_name", out, dtype=pl.String)


def build_side(raw: dict[str, list], spec: TableSpec, side: str, aliases: Aliases, strict: bool,
               machines: pl.DataFrame) -> pl.DataFrame:
    """Driver output of one side -> canonical frame.

    Columns: __source_id, machine_name, machine_order, one canonical text column per mapped
    column, info__* columns, and k__* pairing-key columns plus the occurrence number.
    """
    missing = [c for c in spec.columns if c not in raw]
    if missing:
        raise ValueError(f"{spec.name} / {side}: query result is missing mapped columns {missing}")
    n = len(raw[SOURCE_ID]) if SOURCE_ID in raw else len(next(iter(raw.values()), []))
    cols: list[pl.Series] = [_source_ids(raw.get(SOURCE_ID, [None] * n))]
    for name, col in spec.columns.items():
        s = canonical_series(raw[name], col, side, aliases, strict, spec.datetime_precision)
        cols.append(s.alias(name))
    for c, values in raw.items():
        if c.startswith(INFO_PREFIX):
            cols.append(pl.Series(c, [None if v is None else str(v) for v in values], dtype=pl.String))
    df = pl.DataFrame(cols) if cols else pl.DataFrame()

    id_to_name = dict(zip(machines[spec.machine_key[side]].to_list(), machines["machine_name"].to_list()))
    order = dict(zip(machines["machine_name"].to_list(), machines["machine_order"].to_list()))
    names = _machine_lookup(df["machine"], id_to_name) if n else pl.Series("machine_name", [], dtype=pl.String)
    # The machine is compared by name: the raw ids may legitimately differ between the
    # systems (machines.csv maps them). The raw id is kept as an info column.
    df = df.with_columns(
        names,
        pl.Series("machine_order", [order.get(x, len(order)) for x in names.to_list()], dtype=pl.UInt32),
        pl.col("machine").alias(f"{INFO_PREFIX}machine_id"),
    ).with_columns(pl.col("machine_name").alias("machine"))
    keys = [pl.col("machine_name").alias("k__machine")]
    for k in spec.key[1:]:
        keys.append(pairing_key(df[k], spec.columns[k]).alias(f"k__{k}"))
    df = df.with_columns(keys).sort(SOURCE_ID, nulls_last=True, maintain_order=True)
    key_cols = [f"k__{k}" for k in spec.key]
    return df.with_columns(pl.int_range(pl.len(), dtype=pl.UInt32).over(key_cols).alias(OCC))


# --------------------------------------------------------------------------- comparison

@dataclass
class TableResult:
    spec: TableSpec
    strict: bool
    sides: dict[str, pl.DataFrame]                       # canonical side frames
    records: pl.DataFrame                                # one row per paired / unpaired record
    mismatches: pl.DataFrame                             # one row per differing value
    column_summary: pl.DataFrame
    machine_summary: pl.DataFrame
    known_summary: pl.DataFrame
    totals: dict[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.totals.get("unexplained", 0) == 0


def _side_for_join(df: pl.DataFrame, spec: TableSpec, side: str) -> pl.DataFrame:
    sfx = SUFFIX[side]
    sel = [pl.col(f"k__{k}") for k in spec.key] + [pl.col(OCC)]
    sel += [pl.col("machine_order").alias(f"__order_{sfx}"), pl.lit(True).alias(f"__in_{sfx}")]
    sel += [pl.col(c).alias(f"{c}_{sfx}") for c in spec.columns]
    sel.append(pl.col(SOURCE_ID).alias(f"{side}_source_id"))
    sel += [pl.col(c).alias(f"{sfx}_info_{c[len(INFO_PREFIX):]}") for c in df.columns if c.startswith(INFO_PREFIX)]
    return df.select(sel)


def _known_reason_expr(spec: TableSpec) -> pl.Expr:
    expr = None
    for kd in spec.known_differences:
        sfx = SUFFIX[kd.side]
        label = ONLY_SQL if kd.side == "sqlserver" else ONLY_PG
        cond = pl.col("result") == label
        for col, vals in kd.when.items():
            cond = cond & pl.col(f"{col}_{sfx}").is_in(vals)
        expr = (pl.when(cond) if expr is None else expr.when(cond)).then(pl.lit(kd.reason))
    return (expr.otherwise(None) if expr is not None else pl.lit(None)).cast(pl.String)


def compare_sides(spec: TableSpec, side_sql: pl.DataFrame, side_pg: pl.DataFrame, machines: pl.DataFrame,
                  strict: bool = True) -> TableResult:
    join_on = [f"k__{k}" for k in spec.key] + [OCC]
    a = _side_for_join(side_sql, spec, "sqlserver")
    b = _side_for_join(side_pg, spec, "postgres")
    m = a.join(b, on=join_on, how="full", coalesce=True, nulls_equal=True)
    m = m.with_columns(pl.col("__in_sql").fill_null(False), pl.col("__in_pg").fill_null(False))
    both = pl.col("__in_sql") & pl.col("__in_pg")

    cmp_cols = spec.compare_columns
    ok_exprs = [pl.when(both).then(pl.col(f"{c}_sql").eq_missing(pl.col(f"{c}_pg"))).otherwise(None).alias(f"{c}_ok")
                for c in cmp_cols]
    m = m.with_columns(ok_exprs)
    any_diff = pl.any_horizontal([pl.col(f"{c}_ok") == False for c in cmp_cols]) if cmp_cols else pl.lit(False)  # noqa: E712
    m = m.with_columns(
        pl.when(~pl.col("__in_pg")).then(pl.lit(ONLY_SQL))
          .when(~pl.col("__in_sql")).then(pl.lit(ONLY_PG))
          .when(any_diff.fill_null(False)).then(pl.lit(DIFFERENT))
          .otherwise(pl.lit(IDENTICAL)).alias("result"),
        (pl.concat_list([pl.when(pl.col(f"{c}_ok") == False).then(pl.lit(c)) for c in cmp_cols])  # noqa: E712
           .list.drop_nulls().list.join(", ") if cmp_cols else pl.lit("")).alias("diff_columns"),
        pl.coalesce("__order_sql", "__order_pg").alias("__order"),
    )
    m = m.with_columns(_known_reason_expr(spec).alias("known_reason"))
    m = m.with_columns(
        (pl.col("result").is_in([DIFFERENT, ONLY_SQL, ONLY_PG]) & pl.col("known_reason").is_null()).alias("unexplained"))

    # readable key columns; '<NULL>' (a pairing placeholder) becomes null again
    key_display = [pl.col("k__machine").alias("machine")]
    for k in spec.key[1:]:
        kc = pl.col(f"k__{k}")
        key_display.append(pl.when(kc == NULL_KEY).then(None).otherwise(kc).alias(k))
    m = m.with_columns(key_display)

    sort_cols = ["__order", "machine"] + [k for k in (spec.time_column, "datatype") if k in spec.key] + \
                [k for k in spec.key[1:] if k not in (spec.time_column, "datatype")] + [OCC]
    m = m.sort(sort_cols, nulls_last=True, maintain_order=True)

    lead = ["machine"] + [k for k in sort_cols if k in spec.key[1:]] + [OCC, "result", "diff_columns", "known_reason"]
    value_cols = []
    for c in spec.columns:
        value_cols += [f"{c}_sql", f"{c}_pg"] + ([f"{c}_ok"] if c in cmp_cols else [])
    ids = ["sqlserver_source_id", "postgres_source_id"]
    info = [c for c in m.columns if c.startswith(("sql_info_", "pg_info_"))]
    records = m.select(lead + value_cols + ids + info + ["unexplained", "__order"])

    # ------------------------------------------------------------------ long list of differing values
    key_out = ["machine"] + [k for k in sort_cols if k in spec.key[1:]] + [OCC]
    parts = []
    for c in cmp_cols:
        part = records.filter(pl.col(f"{c}_ok") == False).select(  # noqa: E712
            key_out + [pl.lit(c).alias("column"), pl.col(f"{c}_sql").alias("sqlserver_value"),
                       pl.col(f"{c}_pg").alias("postgres_value")] + ids + ["__order"])
        if part.height:
            parts.append(part)
    if parts:
        mismatches = pl.concat(parts).sort(["__order"] + key_out[1:] + ["column"], maintain_order=True)
    else:
        mismatches = pl.DataFrame(schema={**{k: pl.String for k in key_out}, OCC: pl.UInt32, "column": pl.String,
                                          "sqlserver_value": pl.String, "postgres_value": pl.String,
                                          "sqlserver_source_id": records.schema["sqlserver_source_id"],
                                          "postgres_source_id": records.schema["postgres_source_id"],
                                          "__order": pl.UInt32})

    # ------------------------------------------------------------------ per column
    paired = records.filter(pl.col("result").is_in([IDENTICAL, DIFFERENT])).height
    col_rows = []
    for c in cmp_cols:
        bad = records.filter(pl.col(f"{c}_ok") == False)  # noqa: E712
        ex = ""
        if bad.height:
            r = bad.row(0, named=True)
            ex = f"{r['machine']} {r.get(spec.time_column) or ''}: {r[f'{c}_sql']!r} vs {r[f'{c}_pg']!r}"
        col_rows.append({"column": c, "type": spec.columns[c].type, "records_differing": bad.height,
                         "pct_of_paired": round(100.0 * bad.height / paired, 3) if paired else 0.0,
                         "example (sqlserver vs postgres)": ex})
    column_summary = pl.DataFrame(col_rows, schema={"column": pl.String, "type": pl.String,
                                                    "records_differing": pl.Int64, "pct_of_paired": pl.Float64,
                                                    "example (sqlserver vs postgres)": pl.String})
    column_summary = column_summary.sort("records_differing", descending=True, maintain_order=True)

    # ------------------------------------------------------------------ per machine
    machine_summary = _machine_summary(records, side_sql, side_pg, machines)

    # ------------------------------------------------------------------ known gaps
    known_summary = (records.filter(pl.col("known_reason").is_not_null())
                     .group_by(["known_reason", "result"]).agg(pl.len().alias("records"))
                     .sort("records", descending=True)
                     .rename({"known_reason": "reason", "result": "side"}))
    known_summary = pl.DataFrame({"reason": [kd.reason for kd in spec.known_differences],
                                  "side": [ONLY_SQL if kd.side == "sqlserver" else ONLY_PG
                                           for kd in spec.known_differences]}).join(
        known_summary, on=["reason", "side"], how="left").with_columns(pl.col("records").fill_null(0))

    totals = {r: records.filter(pl.col("result") == r).height for r in RESULTS}
    totals.update({
        "sqlserver_records": side_sql.height, "postgres_records": side_pg.height, "paired": paired,
        "only_sqlserver_known": records.filter((pl.col("result") == ONLY_SQL) & pl.col("known_reason").is_not_null()).height,
        "only_postgres_known": records.filter((pl.col("result") == ONLY_PG) & pl.col("known_reason").is_not_null()).height,
        "unexplained": records.filter(pl.col("unexplained")).height,
        "differing_values": mismatches.height,
    })
    return TableResult(spec=spec, strict=strict, sides={"sqlserver": side_sql, "postgres": side_pg},
                       records=records, mismatches=mismatches, column_summary=column_summary,
                       machine_summary=machine_summary, known_summary=known_summary, totals=totals)


def _machine_summary(records: pl.DataFrame, side_sql: pl.DataFrame, side_pg: pl.DataFrame,
                     machines: pl.DataFrame) -> pl.DataFrame:
    base = machines.select("machine_order", pl.col("machine_name").alias("machine"))
    # machines not in machines.csv: after the listed ones, by name, each with its own order
    extra = (records.select("machine").unique()
             .join(base, on="machine", how="anti")
             .sort("machine")
             .with_row_index("__i")
             .with_columns((pl.col("__i") + len(base)).cast(pl.UInt32).alias("machine_order")))
    base = pl.concat([base, extra.select("machine_order", "machine")])

    rows_sql = side_sql.group_by(pl.col("machine_name").alias("machine")).agg(pl.len().cast(pl.Int64).alias("sqlserver_records"))
    rows_pg = side_pg.group_by(pl.col("machine_name").alias("machine")).agg(pl.len().cast(pl.Int64).alias("postgres_records"))
    agg = records.group_by("machine").agg(
        *[(pl.col("result") == r).sum().cast(pl.Int64).alias(r) for r in RESULTS],
        ((pl.col("result") == ONLY_SQL) & pl.col("known_reason").is_not_null()).sum().cast(pl.Int64).alias("only_sqlserver_known"),
        ((pl.col("result") == ONLY_PG) & pl.col("known_reason").is_not_null()).sum().cast(pl.Int64).alias("only_postgres_known"),
        pl.col("unexplained").sum().cast(pl.Int64).alias("unexplained"),
    )
    out = (base.join(rows_sql, on="machine", how="left").join(rows_pg, on="machine", how="left")
           .join(agg, on="machine", how="left").fill_null(0))
    out = out.with_columns((pl.col(IDENTICAL) + pl.col(DIFFERENT)).alias("paired"))
    known = pl.col("only_sqlserver_known") + pl.col("only_postgres_known")
    out = out.with_columns(
        pl.when((pl.col("sqlserver_records") == 0) & (pl.col("postgres_records") == 0)).then(pl.lit(NO_DATA))
          .when(pl.col("unexplained") > 0).then(pl.lit(FAIL))
          .when(known > 0).then(pl.lit(PASS_KNOWN))
          .otherwise(pl.lit(PASS)).alias("verdict"))
    return out.sort("machine_order", maintain_order=True).select(
        "machine", "verdict", "sqlserver_records", "postgres_records", "paired", IDENTICAL, DIFFERENT,
        ONLY_SQL, "only_sqlserver_known", ONLY_PG, "only_postgres_known", "unexplained")
