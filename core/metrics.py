"""Per-machine counts, totals, coverage and invalid records for each side (Polars).

All numbers are computed from the same canonical side frames the comparison uses, so a
count in the summary always agrees with the records in the workbook.
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from .config import TableSpec
from .compare import SIDES, TableResult
from .db import SOURCE_ID
from .normalize import lenient_expr

NA = "n/a"


@dataclass
class TableMetrics:
    counts: pl.DataFrame        # machine, metric, sqlserver, postgres   (long form, metric order kept)
    coverage: pl.DataFrame      # machine, side, records, first, last
    overlap: pl.DataFrame       # machine, overlap_from, overlap_to, has_overlap
    invalid: pl.DataFrame       # side, machine, time, datatype, source_id, column, value, reason


def _dt_filter(datatypes: list[int] | None) -> pl.Expr:
    if not datatypes:
        return pl.lit(True)
    return pl.col("datatype").is_in([str(d) for d in datatypes])


def _parse_time(col: str) -> pl.Expr:
    return pl.col(col).str.to_datetime("%Y-%m-%d %H:%M:%S%.f", time_unit="us", strict=False)


def _postgres_check(col: str, chk: dict, columns: list[str], strict: bool,
                    is_id: bool = False) -> tuple[pl.Expr, pl.Expr]:
    """Postgres invalid rule for one code column -> (is_invalid, reason). First match wins:

    1. iot id empty            -> no master yet. The stored interface id is still compared, but
                                  the record cannot be aggregated until the master is created.
    2. iot id set, no master   -> the iot id does not resolve in master_data.
    3. master id != record id  -> the iot id is linked to a master with a different interface id.
    """
    v = pl.col(col)
    iot_name, master_name = chk.get("postgres_iot"), chk.get("postgres_master")
    iot = pl.col(f"info__{iot_name}") if iot_name and f"info__{iot_name}" in columns else None
    master = pl.col(f"info__{master_name}") if master_name and f"info__{master_name}" in columns else None
    if iot is None:
        return v.is_null(), pl.lit(f"{col} does not resolve in master_data")

    no_master_yet = iot.is_null()
    unresolved = iot.is_not_null() & (master.is_null() if master is not None else v.is_null())
    if master is not None:
        # the record value `v` was normalised in build_side; the master id must get the
        # same treatment, or lenient mode flags '0007' (master) vs '7' (record) as relinked
        m = master if strict else lenient_expr(master, is_id)
        relinked = iot.is_not_null() & master.is_not_null() & v.is_not_null() & (m != v)
    else:
        m, relinked = None, pl.lit(False)
    reason = (
        pl.when(no_master_yet & v.is_null()).then(pl.lit(f"{col} is empty"))
        .when(no_master_yet).then(pl.concat_str(pl.lit(f"{col} '"), v,
                                                pl.lit("' has no Postgres master yet (iot id empty)")))
        .when(unresolved).then(pl.concat_str(pl.lit(f"{col} iot id "), iot,
                                             pl.lit(" does not resolve in master_data")))
        .otherwise(pl.concat_str(pl.lit(f"{col} iot id "), iot, pl.lit(" is linked to master '"),
                                 m if m is not None else pl.lit(""), pl.lit("' but the record carries '"), v,
                                 pl.lit("'")))
    )
    return no_master_yet | unresolved | relinked, reason


def invalid_records(spec: TableSpec, side: str, df: pl.DataFrame,
                    masters: dict[str, set[str]] | None = None, strict: bool = True) -> pl.DataFrame:
    """Records a side rejected (rawdata status) or whose codes do not resolve (autodata)."""
    tc = spec.time_column
    base = [pl.lit(side).alias("side"), pl.col("machine_name").alias("machine"), pl.col(tc).alias("time"),
            pl.col("datatype"), pl.col(SOURCE_ID).cast(pl.String).alias("source_id"), pl.col("machine_order")]
    parts = []
    st = spec.report.status
    if st:
        good = list(st.get("processed", [])) + list(st.get("not_processed", []))
        col = st["column"]
        parts.append(df.filter(pl.col(col).is_not_null() & ~pl.col(col).is_in(good)).select(
            base + [pl.lit(col).alias("column"), pl.col(col).alias("value"),
                    pl.concat_str(pl.lit("status: "), pl.col(col)).alias("reason")]))
    for col, chk in spec.report.invalid_checks.items():
        sub = df.filter(_dt_filter(chk.get("datatypes")))
        if side == "sqlserver":
            master = (masters or {}).get(col)
            low = pl.col(col).str.strip_chars().str.to_lowercase()
            bad = pl.col(col).is_null() | (pl.col(col).str.strip_chars() == "")
            if master is not None:
                bad = bad | ~low.is_in(list(master))
            reason = (pl.when(pl.col(col).is_null() | (pl.col(col).str.strip_chars() == ""))
                      .then(pl.lit(f"{col} is empty"))
                      .otherwise(pl.lit(f"{col} not in SQL Server master (dropped by the shift aggregation)")))
            value = pl.col(col)
        else:
            bad, reason = _postgres_check(col, chk, sub.columns, strict, spec.columns[col].id)
            value = pl.col(col)
        parts.append(sub.filter(bad).select(base + [pl.lit(col).alias("column"), value.cast(pl.String).alias("value"),
                                                    reason.alias("reason")]))
    schema = {"side": pl.String, "machine": pl.String, "time": pl.String, "datatype": pl.String,
              "source_id": pl.String, "machine_order": pl.UInt32, "column": pl.String, "value": pl.String,
              "reason": pl.String}
    parts = [p.cast(schema) for p in parts if p.height]
    if not parts:
        return pl.DataFrame(schema=schema)
    return pl.concat(parts)


def table_metrics(result: TableResult, machines: pl.DataFrame,
                  sql_masters: dict[str, set[str]] | None = None) -> TableMetrics:
    spec = result.spec
    tc = spec.time_column
    names = machines["machine_name"].to_list()
    extra = sorted({m for s in SIDES for m in result.sides[s]["machine_name"].unique().to_list()} - set(names))
    names += extra

    invalid = pl.concat([invalid_records(spec, "sqlserver", result.sides["sqlserver"], sql_masters),
                         invalid_records(spec, "postgres", result.sides["postgres"], strict=result.strict)])
    invalid = invalid.sort(["machine_order", "time", "side", "column"], nulls_last=True).drop("machine_order")

    # ---------------------------------------------------------------- metric definitions (label, side -> expr)
    defs: list[tuple[str, dict[str, pl.Expr | None]]] = []
    both = lambda e: {"sqlserver": e, "postgres": e}  # noqa: E731
    defs.append(("Total records", both(pl.len())))
    for label, dts in spec.report.counts.items():
        defs.append((label, both(_dt_filter(dts).sum())))
    st = spec.report.status
    if st:
        good_p, good_n = list(st.get("processed", [])), list(st.get("not_processed", []))
        c = pl.col(st["column"])
        defs.append(("Processed", both(c.is_in(good_p).sum())))
        defs.append(("Not processed", both(c.is_in(good_n).sum())))
    # A flag is counted on each side that defines it; a side without a definition shows n/a.
    for label, per_side in spec.report.flags.items():
        defs.append((label, {s: (pl.col(f"info__{f['info']}") == str(f["value"])).sum()
                             for s, f in per_side.items()}))
    for label, s in spec.report.sums.items():
        filt = _dt_filter(s.get("datatypes"))
        if "column" in s:
            e = pl.col(s["column"]).cast(pl.Float64, strict=False).filter(filt).sum()
        else:
            a, b = s["duration"]
            e = ((_parse_time(b) - _parse_time(a)).dt.total_microseconds() / 1_000_000).filter(filt).sum()
        defs.append((label, both(e.round(3))))

    rows = []
    for side in SIDES:
        df = result.sides[side]
        exprs = [(e.cast(pl.Float64) if e is not None else pl.lit(None, dtype=pl.Float64)).alias(f"m{i}")
                 for i, (_, per_side) in enumerate(defs) for e in [per_side.get(side)]]
        g = df.group_by("machine_name").agg(exprs) if df.height else pl.DataFrame(
            schema={"machine_name": pl.String, **{f"m{i}": pl.Float64 for i in range(len(defs))}})
        lookup = {r["machine_name"]: r for r in g.iter_rows(named=True)}
        inv = invalid.filter(pl.col("side") == side).group_by("machine").agg(
            pl.col("source_id").n_unique().alias("n"))
        inv_lookup = dict(zip(inv["machine"].to_list(), inv["n"].to_list()))
        for mname in names:
            r = lookup.get(mname)
            for i, (label, per_side) in enumerate(defs):
                if per_side.get(side) is None:
                    val = NA
                else:
                    val = r[f"m{i}"] if r else 0.0
                    val = 0.0 if val is None else val
                rows.append((mname, i, label, side, _fmt(val)))
            if st or spec.report.invalid_checks:
                rows.append((mname, len(defs), "Invalid records", side, _fmt(float(inv_lookup.get(mname, 0)))))
    long = pl.DataFrame(rows, schema={"machine": pl.String, "idx": pl.Int64, "metric": pl.String,
                                      "side": pl.String, "value": pl.String}, orient="row")
    order = {n: i for i, n in enumerate(names)}
    counts = (long.pivot(on="side", index=["machine", "idx", "metric"], values="value")
              .with_columns(pl.col("machine").replace_strict(order, return_dtype=pl.Int64).alias("__m"))
              .sort(["__m", "idx"]).select("machine", "metric", "sqlserver", "postgres"))

    # ---------------------------------------------------------------- coverage
    cov_rows = []
    for side in SIDES:
        df = result.sides[side]
        g = df.group_by("machine_name").agg(pl.len().alias("records"), pl.col(tc).min().alias("first"),
                                            pl.col(tc).max().alias("last"))
        lookup = {r["machine_name"]: r for r in g.iter_rows(named=True)}
        for mname in names:
            r = lookup.get(mname)
            cov_rows.append({"machine": mname, "side": side, "records": int(r["records"]) if r else 0,
                             "first": r["first"] if r else None, "last": r["last"] if r else None})
    coverage = pl.DataFrame(cov_rows, schema={"machine": pl.String, "side": pl.String, "records": pl.Int64,
                                              "first": pl.String, "last": pl.String})
    ov = []
    for mname in names:
        s = coverage.filter((pl.col("machine") == mname) & (pl.col("side") == "sqlserver")).row(0, named=True)
        p = coverage.filter((pl.col("machine") == mname) & (pl.col("side") == "postgres")).row(0, named=True)
        if s["first"] and p["first"]:
            lo, hi = max(s["first"], p["first"]), min(s["last"], p["last"])
            ov.append({"machine": mname, "overlap_from": lo if lo <= hi else None,
                       "overlap_to": hi if lo <= hi else None, "has_overlap": lo <= hi})
        else:
            ov.append({"machine": mname, "overlap_from": None, "overlap_to": None, "has_overlap": False})
    overlap = pl.DataFrame(ov, schema={"machine": pl.String, "overlap_from": pl.String, "overlap_to": pl.String,
                                       "has_overlap": pl.Boolean})
    return TableMetrics(counts=counts, coverage=coverage, overlap=overlap, invalid=invalid)


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if float(v).is_integer():
        return f"{int(v):,}"
    return f"{v:,.3f}".rstrip("0").rstrip(".")
