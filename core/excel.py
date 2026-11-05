"""The detailed Excel workbook (Polars -> XlsxWriter).

Record sheets are ordered by machine (CSV order), then record time, then datatype. Each
compared column appears three times: <col>_sql, <col>_pg, <col>_ok. A differing value pair
is filled red; the missing side of a one-sided record is filled grey. All values are
written as text, exactly as compared, so ids such as '0001' keep their leading zeros.
"""
from __future__ import annotations

import re
from pathlib import Path

import polars as pl
import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

from .compare import DIFFERENT, IDENTICAL, ONLY_PG, ONLY_SQL, PASS_KNOWN
from .pipeline import RunResult, window_rule

EXCEL_MAX_ROWS = 1_048_575            # one header row + data
SUMMARY_WIDTH = max(22, len(PASS_KNOWN) + 2)   # the longest verdict text must fit
RED, GREY, AMBER, GREEN = "#F8CBAD", "#E7E6E6", "#FFE699", "#C6EFCE"


class _Book:
    def __init__(self, path: Path):
        self.wb = xlsxwriter.Workbook(str(path), {"strings_to_formulas": False, "strings_to_urls": False,
                                                  "strings_to_numbers": False, "nan_inf_to_errors": True})
        self.fmt = {
            "title": self.wb.add_format({"bold": True, "font_size": 14}),
            "h2": self.wb.add_format({"bold": True, "font_size": 12}),
            "wrap": self.wb.add_format({"text_wrap": True, "valign": "top"}),
            "red": self.wb.add_format({"bg_color": RED}),
            "grey": self.wb.add_format({"bg_color": GREY, "font_color": "#7F7F7F"}),
            "amber": self.wb.add_format({"bg_color": AMBER}),
            "green": self.wb.add_format({"bg_color": GREEN}),
        }
        self.notes: list[str] = []
        self.used: set[str] = set()

    def name(self, wanted: str) -> str:
        base = re.sub(r"[\[\]:*?/\\]", "_", wanted)[:31]
        name, i = base, 2
        while name.lower() in self.used:
            suffix = f"~{i}"
            name, i = base[:31 - len(suffix)] + suffix, i + 1
        self.used.add(name.lower())
        return name

    def frame(self, df: pl.DataFrame, sheet: str, position: tuple[int, int] = (0, 0),
              widths: dict[str, int] | None = None, max_width: int = 45) -> tuple[str, int]:
        """Write a frame as an Excel table. Returns (worksheet name, rows written)."""
        if df.height > EXCEL_MAX_ROWS - position[0]:
            keep = EXCEL_MAX_ROWS - position[0]
            self.notes.append(f"Sheet '{sheet}' truncated to the first {keep:,} of {df.height:,} rows.")
            df = df.head(keep)
        if df.width == 0:
            df = pl.DataFrame({"(empty)": []}, schema={"(empty)": pl.String})
        df = _excel_safe(df)
        if widths is None:
            widths = {c: min(max(len(c), *(len(str(v)) for v in df[c].head(200).to_list() if v is not None), 8) + 2,
                             max_width) if df.height else max(len(c) + 2, 8) for c in df.columns}
        # widths are worked out in Excel characters; Polars' column_widths expects pixels
        df.write_excel(workbook=self.wb, worksheet=sheet, position=position, autofit=False,
                       column_widths={c: _chars_to_pixels(w) for c, w in widths.items()},
                       table_style="Table Style Light 1",
                       freeze_panes=(position[0] + 1, 0) if position == (0, 0) else None,
                       header_format={"bold": True, "text_wrap": True, "valign": "top"})
        return sheet, df.height

    def ws(self, sheet: str):
        return self.wb.get_worksheet_by_name(sheet)

    def close(self) -> None:
        self.wb.close()


def _chars_to_pixels(chars: float) -> int:
    """Excel column width in characters (default Calibri 11) -> pixels, as Excel converts it."""
    return int(chars * 7 + 5)


def _excel_safe(df: pl.DataFrame) -> pl.DataFrame:
    """Text for everything except booleans/ints, so values appear exactly as compared."""
    out = []
    for c, dt in df.schema.items():
        if dt == pl.Boolean or dt.is_integer() or dt.is_float():
            out.append(pl.col(c))
        else:
            out.append(pl.col(c).cast(pl.String))
    return df.select(out)


# --------------------------------------------------------------------------- sheets

def _compare_sheet(b: _Book, df: pl.DataFrame, sheet: str, compare_cols: list[str]) -> None:
    df = df.drop("__order", strict=False)
    # wide enough for the header name (up to 30), at least 12 for values; *_ok holds TRUE/FALSE
    widths = {c: max(12, min(len(c) + 2, 30)) for c in df.columns}
    for c in df.columns:
        if c.endswith("_ok"):
            widths[c] = max(8, min(len(c) + 1, 18))
    widths.update({"machine": 18, "st_time": 24, "diff_columns": 28, "known_reason": 40, "result": 15})
    sheet, n = b.frame(df, sheet, widths=widths)
    if n == 0:
        return
    ws = b.ws(sheet)
    last = n                                   # data rows are 1..n (0-based), Excel rows 2..n+1
    cols = {c: i for i, c in enumerate(df.columns)}
    res = xl_col_to_name(cols["result"])
    ws.conditional_format(1, cols["result"], last, cols["result"],
                          {"type": "cell", "criteria": "==", "value": f'"{DIFFERENT}"', "format": b.fmt["red"]})
    for v in (ONLY_SQL, ONLY_PG):
        ws.conditional_format(1, cols["result"], last, cols["result"],
                              {"type": "cell", "criteria": "==", "value": f'"{v}"', "format": b.fmt["amber"]})
    ws.conditional_format(1, cols["result"], last, cols["result"],
                          {"type": "cell", "criteria": "==", "value": f'"{IDENTICAL}"', "format": b.fmt["green"]})
    for c in compare_cols:
        ok = xl_col_to_name(cols[f"{c}_ok"])
        ws.conditional_format(1, cols[f"{c}_sql"], last, cols[f"{c}_pg"], {
            "type": "formula", "criteria": f"=AND(ISLOGICAL(${ok}2),${ok}2=FALSE)", "format": b.fmt["red"]})
    sql_cols = [c for c in df.columns if c.endswith("_sql")]
    pg_cols = [c for c in df.columns if c.endswith("_pg")]
    for side_cols, missing in ((sql_cols, ONLY_PG), (pg_cols, ONLY_SQL)):
        if not side_cols:
            continue
        rng = " ".join(f"{xl_col_to_name(cols[c])}2:{xl_col_to_name(cols[c])}{last + 1}" for c in side_cols)
        first = cols[side_cols[0]]
        ws.conditional_format(1, first, last, first, {
            "type": "formula", "criteria": f'=${res}2="{missing}"', "format": b.fmt["grey"], "multi_range": rng})


def write_workbook(r: RunResult, path: str | Path, sheet_per_machine: bool = False) -> Path:
    path = Path(path)
    b = _Book(path)
    summary = b.name("Summary")
    b.wb.add_worksheet(summary)
    ws = b.ws(summary)

    # record sheets first (so truncation notes are known before the summary is written)
    for name, t in r.tables.items():
        res = t.result
        cmp_cols = res.spec.compare_columns
        if sheet_per_machine:
            for m in res.records["machine"].unique(maintain_order=True).to_list():
                _compare_sheet(b, res.records.filter(pl.col("machine") == m), b.name(f"{name[:4]}_{m}"), cmp_cols)
        else:
            _compare_sheet(b, res.records, b.name(f"{name}_compare"), cmp_cols)
        b.frame(res.mismatches.drop("__order", strict=False), b.name(f"{name}_mismatches"))
        b.frame(t.metrics.invalid, b.name(f"{name}_invalid"))

    cov = pl.concat([t.metrics.coverage.with_columns(pl.lit(n).alias("table")) for n, t in r.tables.items()]) \
        if r.tables else pl.DataFrame()
    if cov.height:
        cov = cov.select("table", "machine", "side", "records", "first", "last")
    b.frame(cov, b.name("coverage"))
    b.frame(r.machines.drop("machine_order", strict=False), b.name("machines"))
    cmap = []
    for name, t in r.tables.items():
        spec = t.result.spec
        for c in spec.columns.values():
            cmap.append({"table": name, "column": c.name, "type": c.type, "compared": c.compare,
                         "pairing_key": c.name in spec.key, "sqlserver": c.sql, "postgres": c.pg})
        for c in spec.not_compared:
            cmap.append({"table": name, "column": c, "type": "", "compared": False, "pairing_key": False,
                         "sqlserver": "(not compared)", "postgres": "(not compared)"})
    b.frame(pl.DataFrame(cmap), b.name("column_map"), max_width=70)

    # ---------------------------------------------------------------- summary sheet
    w = r.window
    ws.write(0, 0, f"OEE migration validation  {r.machine_label}  {w.start:%Y-%m-%d %H:%M} to {w.end:%Y-%m-%d %H:%M}",
             b.fmt["title"])
    ws.write(1, 0, "Overall: " + ("PASS" if r.passed else "FAIL"), b.fmt["h2"])
    row = 3

    def block(title: str, df: pl.DataFrame) -> None:
        nonlocal row
        ws.write(row, 0, title, b.fmt["h2"])
        b.frame(df, summary, position=(row + 1, 0), widths={c: SUMMARY_WIDTH for c in df.columns})
        row += df.height + 4

    info = [("Machine", r.machine_label), ("machines.csv row", str(r.job)),
            ("Logical day", f"{w.start:%Y-%m-%d %H:%M:%S} (incl.) to {w.end:%Y-%m-%d %H:%M:%S} (excl.)"),
            ("SQL Server", r.servers.get("sqlserver", "")), ("Postgres", r.servers.get("postgres", "")),
            ("Compare mode", "strict" if r.strict else "lenient"), ("Run at", f"{r.run_at:%Y-%m-%d %H:%M:%S}")]
    for name, t in r.tables.items():
        rule = window_rule(t.result.spec)
        info += [(f"{name} records (SQL Server)", rule["sqlserver"]), (f"{name} records (Postgres)", rule["postgres"])]
    info += [("Note", n) for n in b.notes]
    block("Run info", pl.DataFrame(info, schema=["item", "value"], orient="row"))
    block("Verdict per machine", r.verdicts)
    for name, t in r.tables.items():
        block(f"{name}: matching", t.result.machine_summary)
        block(f"{name}: counts (SQL Server vs Postgres)", t.metrics.counts)
        block(f"{name}: columns", t.result.column_summary)
        block(f"{name}: known gaps", t.result.known_summary)
    block("Findings", pl.DataFrame({"finding": r.findings or ["(none)"]}))
    ws.set_column(0, 0, 28)
    ws.set_column(1, 12, 22)
    ws.activate()
    b.close()
    return path
