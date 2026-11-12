"""One validation run: machines.csv -> one job per row (machine + logical day) -> compared
tables, metrics and findings per job."""
from __future__ import annotations

import dataclasses
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

import polars as pl

from .compare import (DIFFERENT, FAIL, NO_DATA, ONLY_PG, ONLY_SQL, PASS, PASS_KNOWN, TableResult,
                      build_side, compare_sides)
from .config import (SIDES, WINDOW_CSV_COLUMNS, Aliases, ConfigError, TableSpec, load_aliases, load_connections,
                     load_table_specs)
from .db import (INFO_PREFIX, PG_MACHINES, SOURCE_ID, SQL_MACHINES, SQL_SHIFTS, WINDOW_COLUMNS, Connections, TooManyRows, Window,
                 call_procedure, fetch, fetch_master_set, fetch_side, open_connections, routine_fingerprints,
                 server_info, shift_windows)
from .machines import _by_name, load_machines, resolve_ids
from .metrics import TableMetrics, table_metrics


@dataclass
class TableRun:
    result: TableResult
    metrics: TableMetrics
    datatypes: list[int]
    notes: list[str] = field(default_factory=list)     # extra findings, e.g. unmapped procedure columns


@dataclass
class RunResult:
    window: Window
    machines: pl.DataFrame
    strict: bool
    run_at: datetime
    servers: dict[str, str]
    tables: dict[str, TableRun] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    seconds: float = 0.0
    error: str | None = None         # set when this job could not be compared (e.g. too many rows)
    job: int = 1                     # 1-based row of machines.csv this result belongs to

    @property
    def machine_label(self) -> str:
        return ", ".join(self.machines["machine_name"].to_list())

    @property
    def verdicts(self) -> pl.DataFrame:
        """machine x table verdict, plus unexplained count per table."""
        out = self.machines.select(pl.col("machine_name").alias("machine"))
        for name, t in self.tables.items():
            ms = t.result.machine_summary.select(
                "machine", pl.col("verdict").alias(name), pl.col("unexplained").alias(f"{name}_unexplained"))
            out = out.join(ms, on="machine", how="full", coalesce=True, maintain_order="left")
        return out

    @property
    def passed(self) -> bool:
        return self.error is None and all(t.result.passed for t in self.tables.values())


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def compare_table(spec: TableSpec, raw_sql: dict, raw_pg: dict, machines: pl.DataFrame, aliases: Aliases,
                  strict: bool = True, sql_masters: dict[str, set[str]] | None = None,
                  datatypes: list[int] | None = None) -> TableRun:
    """Compare already-fetched driver output (used by the run and by the tests)."""
    side_sql = build_side(raw_sql, spec, "sqlserver", aliases, strict, machines)
    side_pg = build_side(raw_pg, spec, "postgres", aliases, strict, machines)
    result = compare_sides(spec, side_sql, side_pg, machines, strict)
    return TableRun(result=result, metrics=table_metrics(result, machines, sql_masters),
                    datatypes=list(datatypes if datatypes is not None else spec.default_datatypes))


def window_rule(spec: TableSpec) -> dict[str, str]:
    """How each side selects the records of a logical day, for the reports."""
    if spec.kind == "procedure":
        wins = " and ".join({"shifts": "each shift starting in the logical day (SQL Server dbo.shiftdetails)",
                             "day": "the whole logical day"}[w] for w in spec.windows)
        return {s: f"{' '.join(spec.procedure[s].call.split())}  -  called for {wins}, "
                   "in a transaction that is rolled back" for s in SIDES}
    if spec.window_filter:
        return {s: " ".join(spec.window_filter[s].split()) for s in ("sqlserver", "postgres")}
    rule = f"{spec.time_column} >= start AND {spec.time_column} < end"
    return {"sqlserver": rule, "postgres": rule}


def run(machines_csv: str | Path | pl.DataFrame, tables: list[str] | None = None, strict: bool = True,
        exact_time: bool = False, datatypes: dict[str, list[int]] | None = None,
        conn_cfg: dict | None = None) -> Iterator[RunResult]:
    """One job per machines.csv row (machine + logical day); yields one RunResult per job.

    `machines_csv` is a path to machines.csv, or a machine list already checked by
    core.machines.machines_frame (the UI).

    The machine list is checked once, before any record is read, and the connections are
    shared by all jobs. A job with too many records is yielded with `error` set and the
    run continues with the next row; any other error stops the run.
    """
    specs = load_table_specs()
    tables = tables or list(specs)
    unknown = [t for t in tables if t not in specs]
    if unknown:
        raise KeyError(f"Unknown table(s) {unknown}. Available: {', '.join(specs)}")
    aliases = load_aliases()
    conn_cfg = conn_cfg or load_connections()
    max_rows = int(conn_cfg.get("max_rows_per_side", 1_000_000))

    if isinstance(machines_csv, pl.DataFrame):
        machines = machines_csv
        log(f"{machines.height} row(s) selected")
    else:
        machines = load_machines(machines_csv)
        log(f"{machines.height} row(s) in {machines_csv}")
    conns: Connections = open_connections(conn_cfg)
    try:
        servers = server_info(conns, conn_cfg)
        log(f"SQL Server {servers['sqlserver']} | Postgres {servers['postgres']}")
        machines = resolve_ids(machines, fetch(conns.sqlserver, SQL_MACHINES), fetch(conns.postgres, PG_MACHINES))
        log("machine ids looked up by name in both machine masters")
        run_specs = {n: dataclasses.replace(specs[n], datetime_precision="exact") if exact_time else specs[n]
                     for n in tables}
        sql_masters: dict[str, dict[str, set[str]]] = {}     # per table, read once for all jobs
        check_reviewed(conns, run_specs)                      # procedures: refuse changed definitions
        shifts = (fetch(conns.sqlserver, SQL_SHIFTS)
                  if any(sp.kind == "procedure" and "shifts" in sp.windows for sp in run_specs.values()) else None)

        for i in range(machines.height):
            t0 = time.perf_counter()
            job = machines.slice(i, 1).with_columns(pl.lit(0, dtype=pl.UInt32).alias("machine_order"))
            row = job.row(0, named=True)
            window = Window(row[WINDOW_CSV_COLUMNS[0]], row[WINDOW_CSV_COLUMNS[1]])
            log(f"row {i + 1}/{machines.height}: {row['machine_name']}, "
                f"{window.start:%Y-%m-%d %H:%M:%S} to {window.end:%Y-%m-%d %H:%M:%S}")
            result = RunResult(window=window, machines=job, strict=strict, run_at=datetime.now(),
                               servers=servers, job=i + 1)
            try:
                for name, spec in run_specs.items():
                    if name not in sql_masters:
                        sql_masters[name] = {col: fetch_master_set(conns.sqlserver, chk["sqlserver_master"])
                                             for col, chk in spec.report.invalid_checks.items()
                                             if chk.get("sqlserver_master")}
                    result.tables[name] = _compare_job(conns, spec, name, job, window, aliases, strict,
                                                       sql_masters[name], (datatypes or {}).get(name), max_rows,
                                                       shifts)
            except TooManyRows as exc:
                result.error = str(exc)
                log(f"row {i + 1}: skipped - {exc}")
            if result.error is None:
                result.findings = findings(result)
            result.seconds = time.perf_counter() - t0
            yield result
    finally:
        conns.close()


def _compare_job(conns: Connections, spec: TableSpec, name: str, job: pl.DataFrame, window: Window,
                 aliases: Aliases, strict: bool, sql_masters: dict[str, set[str]],
                 datatypes: list[int] | None, max_rows: int, shifts: dict[str, list] | None = None) -> TableRun:
    dts = datatypes if datatypes is not None else spec.default_datatypes
    if spec.kind == "procedure":
        log(f"  {name}: calling both report procedures per window (rolled back) ...")
        raw_sql, raw_pg = fetch_procedure_job(conns, spec, job, window, shifts)
        notes = unmapped_columns(spec, {"sqlserver": raw_sql, "postgres": raw_pg})
        raw_sql, raw_pg = project(spec, "sqlserver", raw_sql), project(spec, "postgres", raw_pg)
    else:
        ids = {s: job[spec.machine_filter[s]["csv"]].to_list() for s in ("sqlserver", "postgres")}
        log(f"  {name}: reading both databases in parallel ...")
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_sql = pool.submit(fetch_side, conns.sqlserver, spec, "sqlserver", ids["sqlserver"], window, dts,
                                aliases, max_rows)
            f_pg = pool.submit(fetch_side, conns.postgres, spec, "postgres", ids["postgres"], window, dts,
                               aliases, max_rows)
            raw_sql, raw_pg = f_sql.result(), f_pg.result()
    n_sql, n_pg = len(next(iter(raw_sql.values()), [])), len(next(iter(raw_pg.values()), []))
    log(f"  {name}: {n_sql:,} SQL Server and {n_pg:,} Postgres records; comparing ...")
    tr = compare_table(spec, raw_sql, raw_pg, job, aliases, strict, sql_masters, dts)
    if spec.kind == "procedure":
        tr.notes = notes
    t = tr.result.totals
    log(f"  {name}: {t['identical']:,} identical, {t[DIFFERENT]:,} different, "
        f"{t[ONLY_SQL]:,} only SQL Server, {t[ONLY_PG]:,} only Postgres, {t['unexplained']:,} unexplained")
    return tr


# --------------------------------------------------------------------------- report procedures

def check_reviewed(conns: Connections, specs: dict[str, TableSpec]) -> None:
    """Refuse to run a procedure comparison when a routine differs from the reviewed definition."""
    problems = []
    for name, spec in specs.items():
        if spec.kind != "procedure":
            continue
        for side in SIDES:
            reviewed = spec.procedure[side].reviewed
            current = routine_fingerprints(conns.get(side), side, list(reviewed))
            for routine, want in reviewed.items():
                got = current.get(routine)
                if got is None:
                    problems.append(f"{name}: {routine} was not found on {side}")
                elif got != want:
                    problems.append(f"{name}: the definition of {routine} ({side}) changed since it was reviewed "
                                    f"(now {got[:12]}..., reviewed {want[:12]}...). Re-review it and update the "
                                    f"SHA-256 in config/tables/{name}.yaml, or leave '{name}' out of this run.")
    if problems:
        raise ConfigError("Procedure comparison refused:" + "".join("\n  - " + p for p in problems))


def procedure_windows(spec: TableSpec, window: Window,
                      shifts: dict[str, list] | None) -> list[tuple[str, datetime, datetime]]:
    wins: list[tuple[str, datetime, datetime]] = []
    if "shifts" in spec.windows and shifts:
        wins += shift_windows(shifts, window)
    if "day" in spec.windows:
        wins.append(("Logical day", window.start, window.end))
    return wins


def fetch_procedure_job(conns: Connections, spec: TableSpec, job: pl.DataFrame, window: Window,
                        shifts: dict[str, list] | None) -> tuple[dict[str, list], dict[str, list]]:
    """Call both procedures for every window of the job and stack the rows, each tagged with
    window_name / window_start / window_end and a running source id."""
    parts: dict[str, list] = {s: [] for s in SIDES}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for wname, ws, we in procedure_windows(spec, window, shifts):
            futures = {s: pool.submit(call_procedure, conns.get(s), s, spec.procedure[s].call, ws, we,
                                      str(job[spec.procedure[s].machine][0])) for s in SIDES}
            for s, f in futures.items():
                parts[s].append((wname, ws, we, f.result()))
    return _stack(parts["sqlserver"]), _stack(parts["postgres"])


def project(spec: TableSpec, side: str, data: dict[str, list]) -> dict[str, list]:
    """Procedure result columns -> the mapped column names (and info__ columns) of the spec."""
    n = len(data.get(SOURCE_ID, []))
    missing = [c.expr(side) for c in spec.columns.values() if c.expr(side) not in data]
    if missing and n:
        raise ValueError(f"{spec.name} / {side}: the procedure no longer returns {missing}")
    out = {SOURCE_ID: data.get(SOURCE_ID, [])}
    for name, col in spec.columns.items():
        out[name] = data.get(col.expr(side), [None] * n)
    for k, e in spec.info.get(side, {}).items():
        out[INFO_PREFIX + k] = data.get(e, [None] * n)
    return out


def unmapped_columns(spec: TableSpec, data: dict[str, dict[str, list]]) -> list[str]:
    """Returned columns that are neither compared, shown as info, nor listed as not compared."""
    notes = []
    for side, raw in data.items():
        used = {c.expr(side) for c in spec.columns.values()} | set(spec.info.get(side, {}).values())
        used |= set(spec.not_compared) | {SOURCE_ID, *WINDOW_COLUMNS}
        extra = sorted(c for c in raw if c not in used)
        if extra:
            label = "SQL Server" if side == "sqlserver" else "Postgres"
            notes.append(f"{label} returns column(s) that are not mapped yet: {', '.join(extra)}. "
                         f"Add them to config/tables/{spec.name}.yaml.")
    return notes


def _stack(parts: list[tuple[str, datetime, datetime, dict[str, list]]]) -> dict[str, list]:
    data: dict[str, list] = {SOURCE_ID: [], **{c: [] for c in WINDOW_COLUMNS}}
    for *_, raw in parts:
        for c in raw:
            data.setdefault(c, [])
    for wname, ws, we, raw in parts:
        n = len(next(iter(raw.values()), []))
        start = len(data[SOURCE_ID])
        data[SOURCE_ID] += list(range(start + 1, start + n + 1))
        for c, v in zip(WINDOW_COLUMNS, (wname, ws, we)):
            data[c] += [v] * n
        for c in data:
            if c not in (SOURCE_ID, *WINDOW_COLUMNS):
                data[c] += raw.get(c, [None] * n)
    return data


# --------------------------------------------------------------------------- machine list (UI)

def master_list(sql_master: dict[str, list], pg_master: dict[str, list]) -> pl.DataFrame:
    """Every machine name in either master, with its ids and whether it can be compared
    (exactly one row with an id on both sides; the same rule as resolve_ids)."""
    sql = _by_name(sql_master["name"], sql_master["interface_id"])
    pg = _by_name(pg_master["name"], pg_master["interface_id"], pg_master["iot_id"])
    display = {}
    for n in list(sql_master["name"]) + list(pg_master["name"]):
        if n is not None and str(n).strip():
            display.setdefault(str(n).strip().lower(), str(n).strip())
    rows = []
    for key, name in sorted(display.items(), key=lambda kv: kv[1].lower()):
        s_rows, p_rows = sql.get(key, []), pg.get(key, [])
        if not s_rows or not p_rows:
            note = "only in SQL Server" if s_rows else "only in Postgres"
        elif len(s_rows) > 1 or len(p_rows) > 1:
            note = "appears more than once in a master"
        elif not s_rows[0][0] or not p_rows[0][0] or not p_rows[0][1]:
            note = "has no id in a master"
        else:
            note = ""
        rows.append({"machine_name": name,
                     "sqlserver_interface_id": s_rows[0][0] if len(s_rows) == 1 else None,
                     "postgres_interface_id": p_rows[0][0] if len(p_rows) == 1 else None,
                     "postgres_iot_id": p_rows[0][1] if len(p_rows) == 1 else None,
                     "comparable": not note, "note": note})
    return pl.DataFrame(rows, schema={"machine_name": pl.String, "sqlserver_interface_id": pl.String,
                                      "postgres_interface_id": pl.String, "postgres_iot_id": pl.String,
                                      "comparable": pl.Boolean, "note": pl.String})


def master_machines(conn_cfg: dict | None = None) -> tuple[dict[str, str], pl.DataFrame]:
    """Connect, read both machine masters and return (servers, machine list) for the UI."""
    conn_cfg = conn_cfg or load_connections()
    conns = open_connections(conn_cfg)
    try:
        servers = server_info(conns, conn_cfg)
        return servers, master_list(fetch(conns.sqlserver, SQL_MACHINES), fetch(conns.postgres, PG_MACHINES))
    finally:
        conns.close()


# --------------------------------------------------------------------------- findings

_RESOLVE_COLS = {"component": "component_iot_id", "operation": "operation_iot_id",
                 "operator": "operator_iot_id", "down_code": "down_iot_id"}


def findings(r: RunResult) -> list[str]:
    """Plain-language list of what needs attention, most important first."""
    out: list[str] = []
    for name, t in r.tables.items():
        out += [f"{name}: {note}" for note in t.notes]
        res, met = t.result, t.metrics
        cov = met.coverage
        for ms in res.machine_summary.iter_rows(named=True):
            m = ms["machine"]
            if ms["verdict"] == NO_DATA:
                out.append(f"{m} / {name}: no records in either system in this window - nothing was compared.")
                continue
            if ms["sqlserver_records"] == 0 or ms["postgres_records"] == 0:
                has, none = ("SQL Server", "Postgres") if ms["postgres_records"] == 0 else ("Postgres", "SQL Server")
                n = max(ms["sqlserver_records"], ms["postgres_records"])
                out.append(f"{m} / {name}: {n:,} records in {has} but none in {none} for this window. "
                           "The systems hold different periods; choose a window both cover.")
                continue
            ov = met.overlap.filter(pl.col("machine") == m).row(0, named=True)
            if not ov["has_overlap"]:
                c = {row["side"]: row for row in cov.filter(pl.col("machine") == m).iter_rows(named=True)}
                out.append(f"{m} / {name}: the record periods do not overlap (SQL Server {c['sqlserver']['first']} to "
                           f"{c['sqlserver']['last']}, Postgres {c['postgres']['first']} to {c['postgres']['last']}), "
                           f"so no records could pair. Choose a window both systems cover.")
                continue
            if ms[DIFFERENT]:
                rec = res.records.filter((pl.col("machine") == m) & (pl.col("result") == DIFFERENT))
                cols = (rec.select(pl.col("diff_columns").str.split(", ")).explode("diff_columns")
                        .group_by("diff_columns").len().sort(["len", "diff_columns"], descending=[True, False]))
                top = ", ".join(f"`{c}` ({n:,})" for c, n in cols.head(3).iter_rows())
                out.append(f"{m} / {name}: {ms[DIFFERENT]:,} paired records differ, most often in {top}.")
                for col, info in _RESOLVE_COLS.items():
                    iot = f"pg_info_{info}"
                    if f"{col}_ok" not in rec.columns or iot not in rec.columns:
                        continue
                    unresolved = rec.filter((pl.col(f"{col}_ok") == False) & pl.col(f"{col}_pg").is_null()  # noqa: E712
                                            & pl.col(iot).is_not_null())
                    if unresolved.height:
                        ids = ", ".join(sorted(set(unresolved[iot].to_list()))[:5])
                        out.append(f"{m} / {name}: {unresolved.height:,} records have a Postgres {col} iot id "
                                   f"({ids}) that is missing from master_data, so the {col} is empty.")
            for label, side_name in ((ONLY_SQL, "SQL Server"), (ONLY_PG, "Postgres")):
                n_un = res.records.filter((pl.col("machine") == m) & (pl.col("result") == label)
                                          & pl.col("known_reason").is_null()).height
                if n_un:
                    out.append(f"{m} / {name}: {n_un:,} records exist only in {side_name} with no known reason.")
            if ms["verdict"] == PASS_KNOWN:
                out.append(f"{m} / {name}: all differences are known gaps "
                           f"({ms['only_sqlserver_known'] + ms['only_postgres_known']:,} records).")
        inv = met.invalid.group_by(["machine", "side"]).agg(pl.col("source_id").n_unique().alias("n"))
        for m in inv["machine"].unique(maintain_order=True).to_list():
            d = {row["side"]: row["n"] for row in inv.filter(pl.col("machine") == m).iter_rows(named=True)}
            a, b = d.get("sqlserver", 0), d.get("postgres", 0)
            if a != b:
                out.append(f"{m} / {name}: invalid records differ - {a:,} in SQL Server, {b:,} in Postgres "
                           f"(see the {name}_invalid sheet).")
    if not out:
        verdicts = {v for t in r.tables.values() for v in t.result.machine_summary["verdict"].to_list()}
        if verdicts <= {PASS}:
            out.append("All machines match exactly in every compared table.")
    return out


__all__ = ["RunResult", "TableRun", "compare_table", "run", "findings", "window_rule", "master_list", "master_machines",
           "PASS", "FAIL", "NO_DATA", "PASS_KNOWN"]
