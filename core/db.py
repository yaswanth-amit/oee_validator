"""Read-only database access and query building for both sides.

Only SELECT statements are ever sent. Postgres sessions are opened read-only
(default_transaction_read_only). SQL Server has no session-level read-only switch for a
standalone instance, so a dedicated read-only login is recommended (see README).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime

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
