"""Read-only database access and query building for both sides.

Table comparisons send only SELECT statements. Postgres sessions are opened read-only
(default_transaction_read_only). SQL Server has no session-level read-only switch for a
standalone instance, so a dedicated read-only login is recommended (see README).

Procedure comparisons (kind: procedure, e.g. the cockpit) execute one reviewed report
procedure per side. Each call runs in its own transaction that is always rolled back, and it is
refused when the routine's definition differs from the reviewed one (routine_fingerprints).
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Aliases, TableSpec

SOURCE_ID = "__source_id"
INFO_PREFIX = "info__"


class TooManyRows(RuntimeError):
    pass


# --------------------------------------------------------------------------- connections

def _password(cfg: dict) -> str:
    env = cfg.get("password_env")
    if env and os.environ.get(env):
        return os.environ[env]
    raise RuntimeError(
        f"Password not found: set environment variable {env!r} (or put it in oee_validator/.env)")


def connect_sqlserver(cfg: dict):
    import pyodbc

    parts = [
        f"DRIVER={{{cfg.get('driver', 'ODBC Driver 17 for SQL Server')}}}",
        f"SERVER={cfg['server']}",
        f"DATABASE={cfg['database']}",
        f"UID={cfg['user']}",
        "PWD={" + _password(cfg).replace("}", "}}") + "}",
        "ApplicationIntent=ReadOnly",
    ]
    if cfg.get("trust_server_certificate", True):
        parts.append("TrustServerCertificate=yes")
    conn = pyodbc.connect(";".join(parts) + ";", autocommit=True, timeout=15)
    conn.timeout = int(cfg.get("timeout_seconds", 300))
    return conn


def connect_postgres(cfg: dict):
    import psycopg2

    timeout_ms = int(cfg.get("timeout_seconds", 300)) * 1000
    conn = psycopg2.connect(
        host=cfg["host"], port=cfg.get("port", 5432), dbname=cfg["dbname"], user=cfg["user"],
        password=_password(cfg), connect_timeout=15,
        options=f"-c statement_timeout={timeout_ms} -c default_transaction_read_only=on",
        application_name="oee_validator",
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


@dataclass
class Connections:
    sqlserver: object
    postgres: object

    def get(self, side: str):
        return self.sqlserver if side == "sqlserver" else self.postgres

    def close(self) -> None:
        for c in (self.sqlserver, self.postgres):
            try:
                c.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass


def open_connections(conn_cfg: dict) -> Connections:
    sql = connect_sqlserver(conn_cfg["sqlserver"])
    try:
        pg = connect_postgres(conn_cfg["postgres"])
    except Exception:
        sql.close()
        raise
    return Connections(sql, pg)


def fetch(conn, sql: str, params: list | tuple = ()) -> dict[str, list]:
    """Run one SELECT and return {column: [values]} exactly as the driver returned them."""
    cur = conn.cursor()
    try:
        cur.execute(sql, list(params))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        cur.close()
    if not rows:
        return {c: [] for c in cols}
    return {c: list(vals) for c, vals in zip(cols, zip(*rows))}


def server_info(conns: Connections, conn_cfg: dict) -> dict[str, str]:
    """Server and database actually connected to (database names are read from the server)."""
    a = fetch(conns.sqlserver, "SELECT @@SERVERNAME AS s, DB_NAME() AS d")
    b = fetch(conns.postgres, "SELECT current_database() AS d")
    pg = conn_cfg["postgres"]
    return {"sqlserver": f"{a['s'][0]} / {a['d'][0]}",
            "postgres": f"{pg['host']}:{pg.get('port', 5432)} / {b['d'][0]}"}


# --------------------------------------------------------------------------- queries

@dataclass
class Window:
    start: datetime          # inclusive
    end: datetime            # exclusive


def _expr(spec: TableSpec, column: str, side: str, aliases: Aliases) -> str:
    expr = spec.columns[column].expr(side)
    if "{predefined_down_names}" in expr:
        names = ", ".join("'" + n.replace("'", "''") + "'" for n in aliases.predefined_pg_names) or "''"
        expr = expr.replace("{predefined_down_names}", names)
    return expr


_WINDOW_TOKEN = re.compile(r"\{(start|end)\}")


def window_clause(spec: TableSpec, side: str, window: Window, aliases: Aliases) -> tuple[list[str], list]:
    """The window condition of one side, with bound parameters.

    Default: time_column in [start, end). A table can instead define `window_filter`
    (e.g. autodata: the record's cycle range overlaps the logical day); every {start} /
    {end} in it becomes a bound parameter, never inline text.
    """
    ph = "?" if side == "sqlserver" else "%s"
    template = spec.window_filter.get(side)
    if not template:
        t = _expr(spec, spec.time_column, side, aliases)
        return [f"({t}) >= {ph}", f"({t}) < {ph}"], [window.start, window.end]
    params: list = []

    def bind(m: re.Match) -> str:
        params.append(window.start if m.group(1) == "start" else window.end)
        return ph

    return [f"({_WINDOW_TOKEN.sub(bind, template)})"], params


def build_where(spec: TableSpec, side: str, machine_ids: list[str], window: Window,
                datatypes: list[int], aliases: Aliases) -> tuple[str, list]:
    if not machine_ids:
        raise ValueError("at least one machine id is required")
    ph = "?" if side == "sqlserver" else "%s"
    clauses = [f"({spec.machine_filter[side]['expr']}) IN ({', '.join([ph] * len(machine_ids))})"]
    params: list = [str(m) for m in machine_ids]
    wc, wp = window_clause(spec, side, window, aliases)
    clauses += wc
    params += wp
    if datatypes:
        if "datatype" not in spec.columns:
            raise ValueError(f"{spec.name} has no datatype column, so it cannot be filtered by datatype")
        clauses.append(f"({_expr(spec, 'datatype', side, aliases)}) IN ({', '.join([ph] * len(datatypes))})")
        params += [int(d) for d in datatypes]
    return " WHERE " + "\n  AND ".join(clauses), params


def build_select(spec: TableSpec, side: str, machine_ids: list[str], window: Window,
                 datatypes: list[int], aliases: Aliases) -> tuple[str, list]:
    q, qe = ('"', '"') if side == "postgres" else ("[", "]")
    select = [f"{spec.source_id[side]} AS {q}{SOURCE_ID}{qe}"]
    select += [f"{_expr(spec, n, side, aliases)} AS {q}{n}{qe}" for n in spec.columns]
    select += [f"{e} AS {q}{INFO_PREFIX}{n}{qe}" for n, e in spec.info.get(side, {}).items()]
    where, params = build_where(spec, side, machine_ids, window, datatypes, aliases)
    sql = ("SELECT " + ",\n       ".join(select) + f"\nFROM {spec.from_clause[side]}{where}"
           f"\nORDER BY {spec.source_id[side]}")
    return sql, params


def build_count(spec: TableSpec, side: str, machine_ids: list[str], window: Window,
                datatypes: list[int], aliases: Aliases) -> tuple[str, list]:
    where, params = build_where(spec, side, machine_ids, window, datatypes, aliases)
    return f"SELECT COUNT(*) AS n FROM {spec.from_clause[side]}{where}", params


def fetch_side(conn, spec: TableSpec, side: str, machine_ids: list[str], window: Window,
               datatypes: list[int], aliases: Aliases, max_rows: int) -> dict[str, list]:
    sql, params = build_count(spec, side, machine_ids, window, datatypes, aliases)
    n = int(fetch(conn, sql, params)["n"][0])
    if n > max_rows:
        raise TooManyRows(f"{spec.name} / {side}: {n:,} records in the window (limit {max_rows:,}). "
                          "Use a shorter logical day for this row.")
    sql, params = build_select(spec, side, machine_ids, window, datatypes, aliases)
    return fetch(conn, sql, params)


# --------------------------------------------------------------------------- master data

SQL_MACHINES = "SELECT CAST(InterfaceID AS nvarchar(100)) AS interface_id, CAST(machineid AS nvarchar(200)) AS name FROM dbo.machineinformation"
PG_MACHINES = "SELECT interface_id, iot_id::text AS iot_id, machine_id AS name FROM master_data.machine_information"


def fetch_master_set(conn, sql: str) -> set[str]:
    """First column of a master query as a set of trimmed, lower-case codes."""
    data = fetch(conn, sql)
    col = next(iter(data.values()), [])
    return {str(v).strip().lower() for v in col if v is not None}


# --------------------------------------------------------------------------- report procedures

_PROC_TOKEN = re.compile(r"\{(start|end|machine)\}")
WINDOW_COLUMNS = ("window_name", "window_start", "window_end")     # added to every procedure row


def _definition_hash(text: str | None) -> str | None:
    """SHA-256 of a routine definition, ignoring line endings and trailing spaces."""
    if text is None:
        return None
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    norm = "\n".join(line.rstrip() for line in lines).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def routine_fingerprints(conn, side: str, names: list[str]) -> dict[str, str | None]:
    """Current SHA-256 of each routine definition (None = not found). Postgres names are
    schema.function; all overloads of the name are hashed together."""
    out: dict[str, str | None] = {}
    for name in names:
        if side == "sqlserver":
            d = fetch(conn, "SELECT OBJECT_DEFINITION(OBJECT_ID(?)) AS d", [name])["d"]
        else:
            schema, _, fn = name.partition(".")
            d = fetch(conn, "SELECT string_agg(pg_get_functiondef(p.oid), E'\\n' ORDER BY p.oid::regprocedure::text) AS d "
                            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                            "WHERE n.nspname = %s AND p.proname = %s", [schema, fn])["d"]
        out[name] = _definition_hash(d[0] if d else None)
    return out


def call_procedure(conn, side: str, call: str, start: datetime, end: datetime, machine: str) -> dict[str, list]:
    """Execute one reviewed report procedure and return its result as {column: [values]}.

    The call runs in its own transaction that is ALWAYS rolled back, so nothing it writes
    (temp tables, or anything else) persists. SQL Server: the last result set is returned.
    """
    ph = "?" if side == "sqlserver" else "%s"
    params: list = []

    def bind(m: re.Match) -> str:
        params.append({"start": start, "end": end, "machine": machine}[m.group(1)])
        return ph

    sql = _PROC_TOKEN.sub(bind, call)
    cols: list[str] = []
    rows: list = []
    if side == "sqlserver":
        previous = conn.autocommit
        conn.autocommit = False
        cur = conn.cursor()
        try:
            cur.execute("SET NOCOUNT ON; " + sql, params)
            while True:
                if cur.description:
                    cols, rows = [d[0] for d in cur.description], cur.fetchall()
                if not cur.nextset():
                    break
        finally:
            try:
                cur.close()
            finally:
                conn.rollback()
                conn.autocommit = previous
    else:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN READ WRITE")     # the function builds temp tables; rolled back below
            cur.execute(sql, params)
            cols, rows = [d[0] for d in cur.description], cur.fetchall()
        finally:
            try:
                cur.execute("ROLLBACK")
            finally:
                cur.close()
    if not rows:
        return {c: [] for c in cols}
    return {c: list(v) for c, v in zip(cols, zip(*rows))}


SQL_SHIFTS = ("SELECT ShiftName AS name, FromTime AS from_time, ToTime AS to_time, FromDay AS from_day, "
              "ToDay AS to_day FROM dbo.shiftdetails WHERE Running = 1 ORDER BY shiftid")


def shift_windows(shifts: dict[str, list], window: Window) -> list[tuple[str, datetime, datetime]]:
    """The shifts (SQL Server dbo.shiftdetails, Running = 1) that start inside the logical day.
    FromTime / ToTime give the time of day, FromDay / ToDay the day offset."""
    out = []
    base = window.start.date()
    for day in (base - timedelta(days=1), base):
        for name, ft, tt, fd, td in zip(shifts["name"], shifts["from_time"], shifts["to_time"],
                                         shifts["from_day"], shifts["to_day"]):
            start = datetime.combine(day + timedelta(days=int(fd or 0)), ft.time())
            end = datetime.combine(day + timedelta(days=int(td or 0)), tt.time())
            if window.start <= start < window.end and end > start:
                out.append((str(name), start, end))
    return sorted(set(out), key=lambda w: w[1])
