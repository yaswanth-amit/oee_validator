"""Batch validation run: machines.csv -> one Markdown summary + one Excel workbook per row.

Each row of machines.csv is one job: a machine and its logical day
(logical_day_start, logical_day_end). Run from the folder that contains oee_validator/:

    python -m oee_validator --machines oee_validator/machines.csv

Exit code: 0 = every row passes (known gaps allowed), 1 = unexplained differences,
2 = error (bad machine list, connection problem, a row with too many records, ...).
"""
from __future__ import annotations

import argparse
import re
import sys
from contextlib import closing
from pathlib import Path

from .core.config import ConfigError, load_table_specs
from .core.db import TooManyRows, Window
from .core.machines import MachineListError

DEFAULT_OUT = Path(__file__).resolve().parent / "reports"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m oee_validator",
        description="Validate that Postgres holds exactly the same OEE records as SQL Server, "
                    "one report per machines.csv row (machine + logical day).")
    p.add_argument("--machines", required=True,
                   help="CSV: machine_name, sqlserver_interface_id, postgres_interface_id, postgres_iot_id, "
                        "logical_day_start, logical_day_end (YYYY-MM-DD HH:MM:SS; end exclusive)")
    p.add_argument("--tables", nargs="+", help="tables to compare (default: all configured, rawdata then autodata)")
    p.add_argument("--datatypes", nargs="+", metavar="TABLE=1,2,42",
                   help="override the datatypes of a table, e.g. rawdata=1,2,42 (use TABLE=all for no filter)")
    p.add_argument("--lenient", action="store_true", help="trim strings, blank = NULL, ignore leading zeros in ids")
    p.add_argument("--exact-time", action="store_true",
                   help="compare timestamps to the microsecond instead of SQL Server datetime precision")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT), help="folder for the output files")
    p.add_argument("--name", help="file name prefix (default: oee_validation)")
    p.add_argument("--sheet-per-machine", action="store_true", help="one comparison sheet per machine and table")
    return p


def _datatypes(values: list[str] | None, tables: list[str]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for v in values or []:
        if "=" not in v:
            raise argparse.ArgumentTypeError(f"--datatypes expects TABLE=1,2,... (got {v!r})")
        t, dts = v.split("=", 1)
        if t not in tables:
            raise argparse.ArgumentTypeError(f"--datatypes: unknown table {t!r}")
        if dts.strip().lower() == "all":
            out[t] = []                      # empty list = no datatype filter
            continue
        parts = [x.strip() for x in dts.split(",") if x.strip()]
        if not parts:
            raise argparse.ArgumentTypeError(
                f"--datatypes {t}= has no datatypes; give a list such as {t}=1,2,42 or {t}=all")
        try:
            out[t] = [int(x) for x in parts]
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"--datatypes {t}: datatypes must be whole numbers (got {dts!r})") from None
    return out


def stem_for(prefix: str | None, machine: str, window: Window, used: set[str]) -> str:
    """File name stem for one row: <prefix>_<machine>_<start>_to_<end>, unique within the run."""
    safe = re.sub(r"[^\w.-]+", "_", machine).strip("_") or "machine"
    base = f"{prefix or 'oee_validation'}_{safe}_{window.start:%Y-%m-%d_%H%M}_to_{window.end:%Y-%m-%d_%H%M}"
    stem, i = base, 2
    while stem.lower() in used:
        stem, i = f"{base}~{i}", i + 1
    used.add(stem.lower())
    return stem


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    summary: list[dict] = []
    try:
        specs = load_table_specs()
        datatypes = _datatypes(args.datatypes, list(specs))

        # imported late so that --help works without the database drivers
        from .core.excel import write_workbook
        from .core.markdown import render
        from .core.pipeline import log, run

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        used: set[str] = set()
        with closing(run(args.machines, tables=args.tables, strict=not args.lenient,
                         exact_time=args.exact_time, datatypes=datatypes)) as jobs:
            for r in jobs:
                w = r.window
                entry = {"row": str(r.job), "machine": r.machine_label,
                         "from": f"{w.start:%Y-%m-%d %H:%M}", "to": f"{w.end:%Y-%m-%d %H:%M}"}
                if r.error:
                    entry.update({t: "ERROR" for t in (args.tables or specs)})
                    entry["files"] = r.error
                    summary.append(entry)
                    continue
                stem = stem_for(args.name, r.machine_label, w, used)
                md_path = out_dir / f"{stem}.md"
                md_path.write_text(render(r), encoding="utf-8")
                xlsx_path = write_workbook(r, out_dir / f"{stem}.xlsx", sheet_per_machine=args.sheet_per_machine)
                log(f"  written {md_path.name} and {xlsx_path.name}")
                verdicts = r.verdicts.row(0, named=True)
                entry.update({t: verdicts.get(t) or "" for t in r.tables})
                entry["files"] = stem
                entry["_passed"] = r.passed
                summary.append(entry)
    except (MachineListError, ConfigError, TooManyRows, argparse.ArgumentTypeError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit 2
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print()
    print(_plain(summary))
    print()
    errors = sum(1 for e in summary if "_passed" not in e)
    failed = sum(1 for e in summary if e.get("_passed") is False)
    print(f"Rows: {len(summary)}  passed: {len(summary) - errors - failed}  failed: {failed}  errors: {errors}")
    print("Overall:", "PASS" if not errors and not failed else ("ERROR" if errors else "FAIL"))
    print(f"Reports in {Path(args.out_dir).resolve()}")
    return 2 if errors else (1 if failed else 0)


def _plain(summary: list[dict]) -> str:
    if not summary:
        return "(no rows)"
    cols = [c for c in summary[0] if not c.startswith("_")]
    rows = [cols] + [[str(e.get(c, "")) for c in cols] for e in summary]
    w = [max(len(r[i]) for r in rows) for i in range(len(cols))]
    return "\n".join("  ".join(r[i].ljust(w[i]) for i in range(len(cols))) for r in rows)


if __name__ == "__main__":
    sys.exit(main())
