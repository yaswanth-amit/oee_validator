"""Loading and validation of connections, code aliases and table-pair specifications."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = PACKAGE_DIR / "config"
TABLES_DIR = CONFIG_DIR / "tables"

COLUMN_TYPES = {"str", "int", "decimal", "float", "datetime", "date", "bool", "status", "downcode"}
SIDES = ("sqlserver", "postgres")
DATETIME_PRECISIONS = {"exact", "sqlserver_datetime"}
MACHINE_CSV_COLUMNS = ("machine_name", "sqlserver_interface_id", "postgres_interface_id", "postgres_iot_id")
WINDOW_CSV_COLUMNS = ("logical_day_start", "logical_day_end")      # per row: [start, end)


class ConfigError(ValueError):
    pass


def load_dotenv(paths: tuple[Path, ...] = (PACKAGE_DIR / ".env", PACKAGE_DIR.parent / "oee_compare" / ".env")) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win.

    oee_validator/.env is read first; the prototype's oee_compare/.env is a fallback so
    the passwords do not have to be copied around.
    """
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), _unquote(value.strip()))


def _unquote(value: str) -> str:
    """Remove one pair of matching outer quotes only; quotes inside or at one end are kept."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_connections(path: Path = CONFIG_DIR / "connections.yaml") -> dict:
    load_dotenv()
    return _read_yaml(path)


# --------------------------------------------------------------------------- aliases

@dataclass
class Aliases:
    downcodes: dict[str, str]                 # lower-case spelling -> canonical spelling
    status: dict[str, dict[int, str]]         # side -> {code -> meaning}

    @property
    def predefined_pg_names(self) -> list[str]:
        return sorted(self.downcodes)


def load_aliases(path: Path = CONFIG_DIR / "aliases.yaml") -> Aliases:
    raw = _read_yaml(path)
    downcodes: dict[str, str] = {}
    for canonical, spellings in (raw.get("predefined_downcodes") or {}).items():
        canonical = str(canonical)
        downcodes[canonical.lower()] = canonical
        for s in spellings or []:
            downcodes[str(s).lower()] = canonical
    status = {
        side: {int(k): str(v) for k, v in ((raw.get("status") or {}).get(side) or {}).items()}
        for side in SIDES
    }
    return Aliases(downcodes=downcodes, status=status)


# --------------------------------------------------------------------------- table specs

@dataclass
class ColumnSpec:
    name: str
    type: str
    sql: str
    pg: str
    id: bool = False
    compare: bool = True

    def expr(self, side: str) -> str:
        return self.sql if side == "sqlserver" else self.pg


@dataclass
class KnownDifference:
    side: str                                 # the side the record exists on
    when: dict[str, list[str]]                # column -> canonical values
    reason: str


@dataclass
class ReportSpec:
    counts: dict[str, list[int]] = field(default_factory=dict)
    status: dict | None = None
    flags: dict[str, dict] = field(default_factory=dict)
    sums: dict[str, dict] = field(default_factory=dict)
    invalid_checks: dict[str, dict] = field(default_factory=dict)


@dataclass
class TableSpec:
    name: str
    label: str
    order: int
    from_clause: dict[str, str]
    source_id: dict[str, str]
    key: list[str]
    columns: dict[str, ColumnSpec]
    time_column: str
    machine_filter: dict[str, dict[str, str]]
    machine_key: dict[str, str]
    datetime_precision: str = "exact"
    # side -> SQL condition with {start} / {end} placeholders; empty = time_column in [start, end)
    window_filter: dict[str, str] = field(default_factory=dict)
    info: dict[str, dict[str, str]] = field(default_factory=dict)
    default_datatypes: list[int] = field(default_factory=list)
    known_differences: list[KnownDifference] = field(default_factory=list)
    not_compared: list[str] = field(default_factory=list)
    report: ReportSpec = field(default_factory=ReportSpec)

    @property
    def compare_columns(self) -> list[str]:
        return [c.name for c in self.columns.values() if c.compare]


def _need(d: dict, k: str, where: str, source: str):
    if not isinstance(d, dict) or k not in d:
        raise ConfigError(f"{source}: missing '{k}' in {where}")
    return d[k]


def _flag_sides(f) -> dict:
    """A report flag as {side: {info, value}}. Accepts the one-side form {side, info, value}
    and the per-side form {sqlserver: {info, value}, postgres: {info, value}}."""
    if not isinstance(f, dict):
        return {}
    if "side" in f:
        return {f["side"]: {k: f[k] for k in ("info", "value") if k in f}}
    return dict(f)


def parse_table(raw: dict, source: str = "<table>") -> TableSpec:
    columns: dict[str, ColumnSpec] = {}
    for name, c in _need(raw, "columns", "root", source).items():
        ctype = _need(c, "type", f"column {name}", source)
        if ctype not in COLUMN_TYPES:
            raise ConfigError(f"{source}: column {name} has unknown type '{ctype}'")
        columns[name] = ColumnSpec(
            name=name, type=ctype, sql=_need(c, "sql", f"column {name}", source),
            pg=_need(c, "pg", f"column {name}", source),
            id=bool(c.get("id", False)), compare=bool(c.get("compare", True)),
        )
    for required in ("machine", "datatype"):
        if required not in columns:
            raise ConfigError(f"{source}: a '{required}' column is required")

    key = list(_need(raw, "key", "root", source))
    if not key or key[0] != "machine":
        raise ConfigError(f"{source}: key must start with 'machine' (it is replaced by machine_name)")
    for k in key:
        if k not in columns:
            raise ConfigError(f"{source}: key column '{k}' is not defined in columns")

    time_column = raw.get("time_column", "st_time")
    if time_column not in columns or columns[time_column].type != "datetime":
        raise ConfigError(f"{source}: time_column '{time_column}' must be a datetime column")

    machine_filter = {}
    machine_key = {}
    for side in SIDES:
        mf = _need(_need(raw, "machine_filter", "root", source), side, "machine_filter", source)
        _need(mf, "expr", f"machine_filter.{side}", source)
        if _need(mf, "csv", f"machine_filter.{side}", source) not in MACHINE_CSV_COLUMNS:
            raise ConfigError(f"{source}: machine_filter.{side}.csv must be one of {MACHINE_CSV_COLUMNS}")
        machine_filter[side] = {"expr": str(mf["expr"]), "csv": str(mf["csv"])}
        mk = _need(_need(raw, "machine_key", "root", source), side, "machine_key", source)
        if mk not in MACHINE_CSV_COLUMNS:
            raise ConfigError(f"{source}: machine_key.{side} must be one of {MACHINE_CSV_COLUMNS}")
        machine_key[side] = str(mk)

    precision = raw.get("datetime_precision", "exact")
    if precision not in DATETIME_PRECISIONS:
        raise ConfigError(f"{source}: datetime_precision must be one of {sorted(DATETIME_PRECISIONS)}")

    window_filter: dict[str, str] = {}
    if raw.get("window_filter") is not None:
        for side in SIDES:
            cond = str(_need(raw["window_filter"], side, "window_filter", source)).strip()
            if "{start}" not in cond or "{end}" not in cond:
                raise ConfigError(f"{source}: window_filter.{side} must use both {{start}} and {{end}}")
            window_filter[side] = cond

    known = []
    for kd in raw.get("known_differences") or []:
        if kd.get("side") not in SIDES:
            raise ConfigError(f"{source}: known_differences side must be one of {SIDES}")
        for col in kd["when"]:
            if col not in columns:
                raise ConfigError(f"{source}: known_differences refers to unknown column '{col}'")
        known.append(KnownDifference(
            side=kd["side"],
            when={k: [str(v) for v in vals] for k, vals in kd["when"].items()},
            reason=str(kd["reason"]),
        ))

    rep = raw.get("report") or {}
    report = ReportSpec(
        counts={str(k): [int(x) for x in v] for k, v in (rep.get("counts") or {}).items()},
        status=rep.get("status"),
        flags={str(k): _flag_sides(v) for k, v in (rep.get("flags") or {}).items()},
        sums=dict(rep.get("sums") or {}),
        invalid_checks=dict(rep.get("invalid_checks") or {}),
    )
    if report.status and report.status.get("column") not in columns:
        raise ConfigError(f"{source}: report.status.column must be a mapped column")
    for label, s in report.sums.items():
        cols = [s["column"]] if "column" in s else list(s.get("duration") or [])
        if not cols or any(c not in columns for c in cols):
            raise ConfigError(f"{source}: report.sums '{label}' needs a mapped 'column' or 'duration'")
    for col in report.invalid_checks:
        if col not in columns:
            raise ConfigError(f"{source}: report.invalid_checks refers to unknown column '{col}'")

    info = {s: dict((raw.get("info") or {}).get(s) or {}) for s in SIDES}
    for col, chk in report.invalid_checks.items():
        for k in ("postgres_iot", "postgres_master"):
            if chk.get(k) and chk[k] not in info["postgres"]:
                raise ConfigError(f"{source}: invalid_checks.{col}.{k} '{chk[k]}' is not a Postgres info column")
    for label, per_side in report.flags.items():
        if not per_side or any(s not in SIDES or not isinstance(f, dict) or f.get("info") not in info[s]
                               or "value" not in f for s, f in per_side.items()):
            raise ConfigError(f"{source}: report.flags '{label}' needs, per side, an info column of that side "
                              "and a value")

    return TableSpec(
        name=str(_need(raw, "name", "root", source)),
        label=str(raw.get("label", raw["name"])),
        order=int(raw.get("order", 99)),
        from_clause={s: _need(_need(raw, s, "root", source), "from", s, source) for s in SIDES},
        source_id={s: _need(raw[s], "source_id", s, source) for s in SIDES},
        key=key,
        columns=columns,
        time_column=time_column,
        machine_filter=machine_filter,
        machine_key=machine_key,
        datetime_precision=precision,
        window_filter=window_filter,
        info=info,
        default_datatypes=[int(x) for x in raw.get("default_datatypes") or []],
        known_differences=known,
        not_compared=[str(x) for x in raw.get("not_compared") or []],
        report=report,
    )


def load_table_specs(directory: Path = TABLES_DIR) -> dict[str, TableSpec]:
    specs = [parse_table(_read_yaml(p), p.name) for p in sorted(directory.glob("*.yaml"))]
    return {s.name: s for s in sorted(specs, key=lambda s: (s.order, s.name))}
