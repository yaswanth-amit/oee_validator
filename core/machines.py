"""machines.csv: one comparison job per row - a machine, its id in each system, and a logical day.

    machine_name,sqlserver_interface_id,postgres_interface_id,postgres_iot_id,logical_day_start,logical_day_end
    Machine-01,1001,1001,1,2026-10-10 06:00:00,2026-10-11 06:00:00

Each row produces its own Markdown and Excel report. A machine may appear on several rows
(different days), but always with the same ids. Records are paired on machine_name, so a
machine pairs correctly even when its ids differ between the systems. The list is checked
before any data is read.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl

from .config import MACHINE_CSV_COLUMNS, WINDOW_CSV_COLUMNS

CSV_COLUMNS = MACHINE_CSV_COLUMNS + WINDOW_CSV_COLUMNS
_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")


def parse_timestamp(s: str) -> datetime:
    """'2026-10-10 06:00:00' (seconds optional). Other layouts are rejected rather than guessed,
    because a CSV saved from Excel can turn the date into an ambiguous dd/mm or mm/dd form."""
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(s)


class MachineListError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("machines.csv is not valid:\n  - " + "\n  - ".join(problems))


def load_machines(path: str | Path) -> pl.DataFrame:
    """Read and check the CSV. Returns one row per job, in file order, with the logical day
    columns parsed to datetimes."""
    path = Path(path)
    if not path.exists():
        raise MachineListError([f"file not found: {path}"])
    df = pl.read_csv(path, infer_schema=False, encoding="utf8-lossy")
    df = df.rename({c: c.strip().lower() for c in df.columns})
    problems = []
    missing = [c for c in CSV_COLUMNS if c not in df.columns]
    if missing:
        raise MachineListError([f"missing column(s): {', '.join(missing)} "
                                f"(expected {', '.join(CSV_COLUMNS)})"])
    df = (df.select(CSV_COLUMNS)
            .with_columns(pl.col(c).str.strip_chars() for c in CSV_COLUMNS)
            .filter(pl.any_horizontal(pl.col(c).is_not_null() & (pl.col(c) != "") for c in CSV_COLUMNS)))
    if df.height == 0:
        raise MachineListError(["the file has no machines"])

    starts, ends = [], []
    for i, row in enumerate(df.iter_rows(named=True), start=2):
        for c in CSV_COLUMNS:
            if not row[c]:
                problems.append(f"line {i}: '{c}' is empty")
        parsed = {}
        for c in WINDOW_CSV_COLUMNS:
            if row[c]:
                try:
                    parsed[c] = parse_timestamp(row[c])
                except ValueError:
                    problems.append(f"line {i}: {c} {row[c]!r} is not a timestamp (use YYYY-MM-DD HH:MM:SS)")
        s, e = parsed.get(WINDOW_CSV_COLUMNS[0]), parsed.get(WINDOW_CSV_COLUMNS[1])
        if s and e and e <= s:
            problems.append(f"line {i}: logical_day_end {row[WINDOW_CSV_COLUMNS[1]]} is not after "
                            f"logical_day_start {row[WINDOW_CSV_COLUMNS[0]]}")
        starts.append(s)
        ends.append(e)

    # a machine may repeat (one row per day), but its ids must not change between rows,
    # and one id must not belong to two machines
    ids = df.select(MACHINE_CSV_COLUMNS).filter(pl.all_horizontal(pl.col(c).is_not_null() & (pl.col(c) != "")
                                                                  for c in MACHINE_CSV_COLUMNS)).unique(maintain_order=True)
    for c in MACHINE_CSV_COLUMNS:
        dups = ids.filter(pl.col(c).is_duplicated())[c].unique(maintain_order=True).to_list()
        if dups and c == "machine_name":
            problems.append(f"machine_name with different ids on different rows: {', '.join(dups)}")
        elif dups:
            problems.append(f"{c} used by more than one machine: {', '.join(dups)}")
    rep = df.filter(pl.struct(["machine_name", *WINDOW_CSV_COLUMNS]).is_duplicated())
    if rep.height:
        names = rep.select(pl.concat_str(["machine_name", *WINDOW_CSV_COLUMNS], separator=" ")).unique(
            maintain_order=True).to_series().to_list()
        problems.append(f"the same machine and logical day appear more than once: {'; '.join(names)}")
    if problems:
        raise MachineListError(problems)
    df = df.with_columns(pl.Series(WINDOW_CSV_COLUMNS[0], starts, dtype=pl.Datetime("us")),
                         pl.Series(WINDOW_CSV_COLUMNS[1], ends, dtype=pl.Datetime("us")))
    return df.with_row_index("machine_order")


def check_against_masters(machines: pl.DataFrame, sql_master: dict[str, list],
                          pg_master: dict[str, list]) -> pl.DataFrame:
    """Add found/name columns from both machine masters. Raises if an id is missing or the
    Postgres iot id belongs to a different interface id than the one in the CSV."""
    sql = {str(i).strip(): n for i, n in zip(sql_master["interface_id"], sql_master["name"]) if i is not None}
    pg_by_if = {str(i).strip(): (str(t), n) for i, t, n in
                zip(pg_master["interface_id"], pg_master["iot_id"], pg_master["name"]) if i is not None}
    pg_by_iot = {str(t).strip(): str(i).strip() for i, t in
                 zip(pg_master["interface_id"], pg_master["iot_id"]) if t is not None and i is not None}
    problems, rows = [], []
    for r in machines.iter_rows(named=True):
        name = r["machine_name"]
        s_id, p_id, p_iot = r["sqlserver_interface_id"], r["postgres_interface_id"], r["postgres_iot_id"]
        in_sql, in_pg, iot_ok = s_id in sql, p_id in pg_by_if, p_iot in pg_by_iot
        if not in_sql:
            problems.append(f"{name}: SQL Server interface id {s_id!r} is not in dbo.machineinformation")
        if not in_pg:
            problems.append(f"{name}: Postgres interface id {p_id!r} is not in master_data.machine_information")
        if not iot_ok:
            problems.append(f"{name}: Postgres iot id {p_iot!r} is not in master_data.machine_information")
        elif pg_by_iot[p_iot] != p_id:
            problems.append(f"{name}: Postgres iot id {p_iot} belongs to interface id "
                            f"{pg_by_iot[p_iot]!r}, not {p_id!r}")
        rows.append({**r, "sqlserver_name": sql.get(s_id), "postgres_name": pg_by_if.get(p_id, (None, None))[1],
                     "found_in_sqlserver": in_sql, "found_in_postgres": in_pg and iot_ok})
    if problems:
        raise MachineListError(list(dict.fromkeys(problems)))     # a machine on several rows: report once
    return pl.DataFrame(rows, schema_overrides={"machine_order": pl.UInt32})


def id_to_name(machines: pl.DataFrame, csv_column: str) -> dict[str, str]:
    """Map an id column of the CSV to machine_name (ids compared trimmed, leading zeros kept)."""
    return dict(zip(machines[csv_column].to_list(), machines["machine_name"].to_list()))
