"""machines.csv: one comparison job per row - a machine name and a logical day.

    machine_name,logical_day_start,logical_day_end
    Machine-01,2026-10-10 06:00:00,2026-10-11 06:00:00

Each row produces its own Markdown and Excel report. A machine may appear on several rows
(different days). The machine's ids are not in the file: they are looked up by name in both
machine masters (SQL Server dbo.machineinformation.machineid, Postgres
master_data.machine_information.machine_id) before any data is read. Records are paired on
machine_name, so a machine pairs correctly even when its ids differ between the systems.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl

from .config import WINDOW_CSV_COLUMNS

CSV_COLUMNS = ("machine_name",) + WINDOW_CSV_COLUMNS
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
    columns parsed to datetimes. Extra columns (such as the old id columns) are ignored."""
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

    rep = df.filter(pl.struct(list(CSV_COLUMNS)).is_duplicated())
    if rep.height:
        names = rep.select(pl.concat_str(list(CSV_COLUMNS), separator=" ")).unique(
            maintain_order=True).to_series().to_list()
        problems.append(f"the same machine and logical day appear more than once: {'; '.join(names)}")
    if problems:
        raise MachineListError(problems)
    df = df.with_columns(pl.Series(WINDOW_CSV_COLUMNS[0], starts, dtype=pl.Datetime("us")),
                         pl.Series(WINDOW_CSV_COLUMNS[1], ends, dtype=pl.Datetime("us")))
    return df.with_row_index("machine_order")


def _by_name(names: list, *cols: list) -> dict[str, list[tuple]]:
    """Master rows keyed by trimmed, case-insensitive machine name (a name may repeat)."""
    out: dict[str, list[tuple]] = {}
    for n, *vals in zip(names, *cols):
        if n is not None:
            out.setdefault(str(n).strip().lower(), []).append(
                tuple(None if v is None else str(v).strip() for v in vals))
    return out


def resolve_ids(machines: pl.DataFrame, sql_master: dict[str, list],
                pg_master: dict[str, list]) -> pl.DataFrame:
    """Look up each machine_name in both machine masters and add its ids:
    sqlserver_interface_id, postgres_interface_id and postgres_iot_id, plus the master names.
    Raises if a name is missing from a master, appears more than once, or has no id."""
    sql = _by_name(sql_master["name"], sql_master["interface_id"])
    pg = _by_name(pg_master["name"], pg_master["interface_id"], pg_master["iot_id"])
    sql_names = {str(n).strip().lower(): str(n) for n in sql_master["name"] if n is not None}
    pg_names = {str(n).strip().lower(): str(n) for n in pg_master["name"] if n is not None}
    problems, rows = [], []
    for r in machines.iter_rows(named=True):
        name = r["machine_name"]
        key = name.strip().lower()
        s_rows, p_rows = sql.get(key, []), pg.get(key, [])
        if not s_rows:
            problems.append(f"{name}: not found in SQL Server dbo.machineinformation (machineid)")
        elif len(s_rows) > 1:
            problems.append(f"{name}: appears {len(s_rows)} times in SQL Server dbo.machineinformation")
        elif not s_rows[0][0]:
            problems.append(f"{name}: has no InterfaceID in SQL Server dbo.machineinformation")
        if not p_rows:
            problems.append(f"{name}: not found in Postgres master_data.machine_information (machine_id)")
        elif len(p_rows) > 1:
            problems.append(f"{name}: appears {len(p_rows)} times in Postgres master_data.machine_information")
        elif not p_rows[0][0] or not p_rows[0][1]:
            problems.append(f"{name}: has no interface_id or iot_id in Postgres master_data.machine_information")
        s_id = s_rows[0][0] if len(s_rows) == 1 else None
        p_if, p_iot = p_rows[0] if len(p_rows) == 1 else (None, None)
        rows.append({**r, "sqlserver_interface_id": s_id, "postgres_interface_id": p_if, "postgres_iot_id": p_iot,
                     "sqlserver_name": sql_names.get(key), "postgres_name": pg_names.get(key),
                     "found_in_sqlserver": bool(s_id), "found_in_postgres": bool(p_if and p_iot)})
    if problems:
        raise MachineListError(list(dict.fromkeys(problems)))     # a machine on several rows: report once
    return pl.DataFrame(rows, schema_overrides={"machine_order": pl.UInt32})


def id_to_name(machines: pl.DataFrame, csv_column: str) -> dict[str, str]:
    """Map an id column (resolved from the masters) to machine_name."""
    return dict(zip(machines[csv_column].to_list(), machines["machine_name"].to_list()))
