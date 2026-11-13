"""OEE Validator - screens for the SQL Server vs Postgres comparison.

The same engine and the same results as the command line and its reports, shown as screens:
pick machines (read from both machine masters), a From / To time window and tables, run, then browse the
overview, each machine-day, each table and each record. The Markdown and Excel reports can be
downloaded from the screens or saved to the reports folder, exactly as the command line writes them.

Start from the folder that contains oee_validator/:

    streamlit run oee_validator/ui.py --server.address localhost

Only SELECT statements are sent (see core/db.py); passwords come from oee_validator/.env.
"""
from __future__ import annotations

import io
import sys
import tempfile
import zipfile
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # so `oee_validator` imports under streamlit

import polars as pl  # noqa: E402
import streamlit as st  # noqa: E402

from oee_validator.core import pipeline  # noqa: E402
from oee_validator.core.compare import (DIFFERENT, FAIL, IDENTICAL, NO_DATA, OCC, ONLY_PG, ONLY_SQL,  # noqa: E402
                                        PASS, PASS_KNOWN, RESULTS)
from oee_validator.core.config import load_table_specs  # noqa: E402
from oee_validator.core.excel import write_workbook  # noqa: E402
from oee_validator.core.machines import MachineListError, machines_frame  # noqa: E402
from oee_validator.core.markdown import render  # noqa: E402
from oee_validator.run import DEFAULT_OUT, stem_for  # noqa: E402

MAX_JOBS = 100
ICON = {PASS: "✅", PASS_KNOWN: "🟡", FAIL: "❌", NO_DATA: "⚪", "ERROR": "⛔"}
RESULT_ICON = {IDENTICAL: "✅ identical", DIFFERENT: "❌ different", ONLY_SQL: "🟠 only SQL Server",
               ONLY_PG: "🟣 only Postgres"}
COMMON_DATATYPES = [1, 2, 11, 22, 40, 41, 42, 62]


def window_parts(start: datetime | None, end: datetime | None, split: bool) -> list[tuple[datetime, datetime]]:
    """[start, end) as one window, or split into 24-hour logical days from the start time
    (the last one may be shorter). Empty when the window is incomplete or end <= start."""
    if not start or not end or end <= start:
        return []
    if not split:
        return [(start, end)]
    parts, s = [], start
    while s < end:
        parts.append((s, min(s + timedelta(days=1), end)))
        s += timedelta(days=1)
    return parts


def badge(verdict: str | None) -> str:
    return f"{ICON.get(verdict, '')} {verdict}".strip() if verdict else ""


def day_label(r: pipeline.RunResult) -> str:
    return f"{r.window.start:%Y-%m-%d %H:%M} → {r.window.end:%Y-%m-%d %H:%M}"


def show(df: pl.DataFrame, **kw):
    """A read-only table; internal helper columns are hidden."""
    df = df.drop([c for c in df.columns if c.startswith("__")])
    return st.dataframe(df, width="stretch", hide_index=True, **kw)


# --------------------------------------------------------------------------- data

@st.cache_data(ttl=600, show_spinner="Reading the machine masters of both databases ...")
def load_master() -> tuple[dict[str, str], pl.DataFrame]:
    return pipeline.master_machines()


def xlsx_bytes(r: pipeline.RunResult, sheet_per_machine: bool) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = write_workbook(r, Path(tmp) / "report.xlsx", sheet_per_machine=sheet_per_machine)
        return path.read_bytes()


def report_files(results: list[pipeline.RunResult], sheet_per_machine: bool) -> list[tuple[str, str, bytes]]:
    """(stem, extension, content) for every compared job, named exactly as the command line names them."""
    used: set[str] = set()
    out = []
    for r in results:
        if r.error:
            continue
        stem = stem_for(None, r.machine_label, r.window, used)
        out.append((stem, "md", render(r).encode("utf-8")))
        out.append((stem, "xlsx", xlsx_bytes(r, sheet_per_machine)))
    return out


# --------------------------------------------------------------------------- page

st.set_page_config(page_title="OEE Validator", page_icon="📊", layout="wide")
specs = load_table_specs()

with st.sidebar:
    st.header("Run settings")
    try:
        servers, master = load_master()
    except Exception as exc:  # noqa: BLE001 - show any connection problem on the screen
        st.error(f"Could not read the machine masters: {type(exc).__name__}: {exc}")
        st.caption("Check oee_validator/.env (passwords) and config/connections.yaml (servers).")
        if st.button("Try again"):
            load_master.clear()
            st.rerun()
        st.stop()

    st.caption(f"**SQL Server:** {servers['sqlserver']}  \n**Postgres:** {servers['postgres']}")
    if st.button("Refresh machine list", help="Read both machine masters again"):
        load_master.clear()
        st.rerun()

    comparable = master.filter(pl.col("comparable"))["machine_name"].to_list()
    machines_sel = st.multiselect(
        "Machines", comparable, placeholder="Choose machines",
        help="Machines found once, with ids, in both machine masters (SQL Server dbo.machineinformation, "
             "Postgres master_data.machine_information).")
    blocked = master.filter(~pl.col("comparable"))
    if blocked.height:
        with st.expander(f"{blocked.height} machine(s) cannot be compared"):
            show(blocked.select("machine_name", "note"))

    st.subheader("Time window")
    yesterday = date.today() - timedelta(days=1)
    c_fd, c_ft = st.columns([3, 2])
    from_date = c_fd.date_input("From date", value=yesterday, format="YYYY-MM-DD")
    from_time = c_ft.time_input("From time", value=time(6, 0), step=timedelta(minutes=30))
    c_td, c_tt = st.columns([3, 2])
    to_date = c_td.date_input("To date", value=yesterday + timedelta(days=1), format="YYYY-MM-DD")
    to_time = c_tt.time_input("To time", value=time(6, 0), step=timedelta(minutes=30))
    split_days = st.checkbox("One comparison per day", value=True,
                             help="On: a window longer than 24 h is split into 24-hour logical days starting at "
                                  "the From time. Off: the whole window is one comparison.")
    st.caption("The window is [From, To): From is included, To is not.")

    st.subheader("Tables")
    tables_sel = st.multiselect("Tables to compare", list(specs), default=list(specs),
                                format_func=lambda n: f"{n}  ({specs[n].label.split('-', 1)[-1].strip()})")
    datatypes: dict[str, list[int]] = {}
    for n in tables_sel:
        if "datatype" in specs[n].columns:
            opts = sorted(set(COMMON_DATATYPES) | set(specs[n].default_datatypes))
            datatypes[n] = st.multiselect(f"{n} datatypes", opts, default=specs[n].default_datatypes,
                                          help="Leave empty to compare every datatype.")

    with st.expander("Options"):
        lenient = st.checkbox("Lenient compare", value=True, help="Trim spaces, treat blank as NULL, ignore leading zeros in ids")
        exact_time = st.checkbox("Exact time (microseconds)",
                                 help="Off: Postgres timestamps are rounded to SQL Server datetime steps (1/300 s)")
        sheet_per_machine = st.checkbox("Excel: one comparison sheet per machine")
        save_to_folder = st.checkbox("Also save the reports to the reports folder", value=False,
                                     help=str(DEFAULT_OUT))

    w_from = datetime.combine(from_date, from_time) if from_date and from_time else None
    w_to = datetime.combine(to_date, to_time) if to_date and to_time else None
    days = window_parts(w_from, w_to, split_days)
    if w_from and w_to and w_to <= w_from:
        st.error("To must be after From.")
    n_jobs = len(machines_sel) * len(days)
    st.caption(f"{len(machines_sel)} machine(s) × {len(days)} window(s) = **{n_jobs} comparison(s)**")
    run_clicked = st.button("Run comparison", type="primary", width="stretch",
                            disabled=not (machines_sel and days and tables_sel) or n_jobs > MAX_JOBS)
    if n_jobs > MAX_JOBS:
        st.warning(f"At most {MAX_JOBS} comparisons per run. Choose fewer machines or a shorter window.")


# --------------------------------------------------------------------------- run

if run_clicked:
    rows = [(m, s, e) for m in machines_sel for s, e in days]
    try:
        job_list = machines_frame(rows)
    except MachineListError as exc:
        st.error(str(exc))
        st.stop()
    results: list[pipeline.RunResult] = []
    bar = st.progress(0.0, text="Connecting ...")
    try:
        with closing(pipeline.run(job_list, tables=tables_sel, strict=not lenient, exact_time=exact_time,
                                  datatypes=datatypes)) as jobs:
            for i, r in enumerate(jobs, start=1):
                results.append(r)
                bar.progress(i / len(rows), text=f"{i}/{len(rows)} done: {r.machine_label}, {day_label(r)}")
    except MachineListError as exc:
        st.error(str(exc))
        st.stop()
    except Exception as exc:  # noqa: BLE001 - keep the jobs that finished, show the error
        st.error(f"The run stopped: {type(exc).__name__}: {exc}")
    bar.empty()
    st.session_state.update(results=results, sheet_per_machine=sheet_per_machine, zip_bytes=None,
                            xlsx_cache={}, saved_to=None)
    if save_to_folder and results:
        DEFAULT_OUT.mkdir(parents=True, exist_ok=True)
        for stem, ext, content in report_files(results, sheet_per_machine):
            (DEFAULT_OUT / f"{stem}.{ext}").write_bytes(content)
        st.session_state.saved_to = str(DEFAULT_OUT)

results: list[pipeline.RunResult] = st.session_state.get("results") or []

st.title("OEE Validator: SQL Server vs Postgres")
if not results:
    st.info("Choose machines, the time window and tables on the left, then press **Run comparison**.\n\n"
            "Every record of each machine and logical day is paired between the two databases and every mapped "
            "column is compared. The results appear here; the Markdown and Excel reports can be downloaded too.")
    st.stop()


# --------------------------------------------------------------------------- overview

tables = list(next((r.tables for r in results if not r.error), {}) or tables_sel)
over_rows = []
for i, r in enumerate(results):
    row = {"#": i + 1, "Machine": r.machine_label, "Logical day": day_label(r)}
    if r.error:
        row.update({t: badge("ERROR") for t in tables})
        row.update({"Unexplained": None, "Overall": badge("ERROR")})
    else:
        v = r.verdicts.row(0, named=True)
        row.update({t: badge(v.get(t)) for t in tables})
        row["Unexplained"] = sum(int(v.get(f"{t}_unexplained") or 0) for t in tables)
        row["Overall"] = badge(PASS if r.passed else FAIL)
    over_rows.append(row)
overview = pl.DataFrame(over_rows, infer_schema_length=None)

n_err = sum(1 for r in results if r.error)
n_fail = sum(1 for r in results if not r.error and not r.passed)
c = st.columns(4)
c[0].metric("Comparisons", len(results))
c[1].metric("Passed", len(results) - n_fail - n_err)
c[2].metric("Failed", n_fail)
c[3].metric("Errors", n_err)

st.subheader("Overview")
st.caption("✅ PASS = every record matches · 🟡 PASS (known gaps only) · ❌ FAIL = at least one unexplained "
           "difference · ⚪ NO DATA · ⛔ ERROR. **Click a row** to see its details below.")
event = show(overview, on_select="rerun", selection_mode="single-row", key="overview")
sel = event.selection.rows if event and event.selection else []
job = sel[0] if sel else 0

d1, d2, d3 = st.columns([1, 1, 2])
if d1.button("Prepare all reports (.zip)"):
    with st.spinner("Writing the reports ..."):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for stem, ext, content in report_files(results, st.session_state.get("sheet_per_machine", False)):
                z.writestr(f"{stem}.{ext}", content)
        st.session_state.zip_bytes = buf.getvalue()
if st.session_state.get("zip_bytes"):
    d2.download_button("Download all reports", st.session_state.zip_bytes, file_name="oee_validation_reports.zip",
                       mime="application/zip")
if st.session_state.get("saved_to"):
    d3.caption(f"Reports also saved to {st.session_state.saved_to}")


# --------------------------------------------------------------------------- one machine-day

r = results[job]
st.divider()
st.header(f"{r.machine_label} · {day_label(r)}")
if r.error:
    st.error(f"Not compared: {r.error}")
    st.stop()

if r.passed:
    st.success("**PASS.** Every record matches in every compared table (known gaps excluded).")
else:
    st.error("**FAIL.** At least one table has unexplained differences - see Findings and the table views.")

f1, f2, _ = st.columns([1, 1, 3])
f1.download_button("Download Markdown report", render(r).encode("utf-8"),
                   file_name=f"{stem_for(None, r.machine_label, r.window, set())}.md", mime="text/markdown")
cache = st.session_state.setdefault("xlsx_cache", {})
if job not in cache:
    cache[job] = xlsx_bytes(r, st.session_state.get("sheet_per_machine", False))
f2.download_button("Download Excel report", cache[job],
                   file_name=f"{stem_for(None, r.machine_label, r.window, set())}.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# A view selector instead of st.tabs: it keeps its choice when a click reruns the page
# (tabs jump back to the first one, which hid the record detail).
views = ["Summary"] + list(r.tables) + ["Run info & mapping"]
view = st.segmented_control("View", views, default="Summary", key=f"view_{job}",
                            label_visibility="collapsed") or "Summary"

# ---------------- summary
if view == "Summary":
    st.subheader("Findings")
    st.markdown("\n".join(f"- {f}" for f in r.findings) or "No findings.")

    st.subheader("Verdict per table")
    vrows = []
    for name, t in r.tables.items():
        for ms in t.result.machine_summary.iter_rows(named=True):
            vrows.append({"Table": name, "Machine": ms["machine"], "Verdict": badge(ms["verdict"]),
                          "SQL Server records": ms["sqlserver_records"], "Postgres records": ms["postgres_records"],
                          "Paired": ms["paired"], "Identical": ms[IDENTICAL], "Different": ms[DIFFERENT],
                          "Only SQL Server": ms[ONLY_SQL], "…of which known": ms["only_sqlserver_known"],
                          "Only Postgres": ms[ONLY_PG], "…of which known ": ms["only_postgres_known"],
                          "Unexplained": ms["unexplained"]})
    show(pl.DataFrame(vrows))

    st.subheader("Data coverage")
    st.caption("First and last record time inside the logical day, per system.")
    crows = []
    for name, t in r.tables.items():
        cov, ov = t.metrics.coverage, t.metrics.overlap
        for m in ov["machine"].to_list():
            s = cov.filter((pl.col("machine") == m) & (pl.col("side") == "sqlserver")).row(0, named=True)
            p = cov.filter((pl.col("machine") == m) & (pl.col("side") == "postgres")).row(0, named=True)
            o = ov.filter(pl.col("machine") == m).row(0, named=True)
            crows.append({"Table": name, "Machine": m,
                          "SQL Server records": s["records"], "SQL Server first": s["first"], "SQL Server last": s["last"],
                          "Postgres records": p["records"], "Postgres first": p["first"], "Postgres last": p["last"],
                          "Overlap": f"{o['overlap_from']} to {o['overlap_to']}" if o["has_overlap"] else "none"})
    show(pl.DataFrame(crows))

    st.subheader("Known (by-design) differences")
    st.caption("Records that exist in one system only by design. They are listed but do not fail a machine.")
    krows = [{"Table": n, "Rule": x["reason"], "Present only in": "SQL Server" if x["side"] == ONLY_SQL else "Postgres",
              "Records explained": x["records"]}
             for n, t in r.tables.items() for x in t.result.known_summary.iter_rows(named=True)]
    if krows:
        show(pl.DataFrame(krows))
    else:
        st.caption("No known-difference rules are configured.")

    st.caption("*Unexplained* = paired records with a differing value plus one-sided records with no known reason. "
               "Totals are for orientation; the record comparison itself is exact.")

# ---------------- one tab per table
for name, t in r.tables.items():
    if view == name:
        res, met, spec = t.result, t.metrics, t.result.spec
        st.caption(spec.label)
        tot = res.totals
        m = st.columns(8)
        for col, (label, key) in zip(m, [("SQL Server records", "sqlserver_records"), ("Postgres records", "postgres_records"),
                                         ("Paired", "paired"), ("Identical", IDENTICAL), ("Different", DIFFERENT),
                                         ("Only SQL Server", ONLY_SQL), ("Only Postgres", ONLY_PG),
                                         ("Unexplained", "unexplained")]):
            col.metric(label, f"{tot.get(key, 0):,}")

        st.subheader("Counts and totals (SQL Server / Postgres)")
        counts = met.counts.with_columns(
            pl.when(pl.col("sqlserver") == pl.col("postgres")).then(pl.lit("✅")).otherwise(pl.lit("❌")).alias("match"))
        show(counts.rename({"machine": "Machine", "metric": "Metric", "sqlserver": "SQL Server", "postgres": "Postgres",
                            "match": "Match"}))

        st.subheader("Columns with differences")
        cs = res.column_summary.filter(pl.col("records_differing") > 0)
        if cs.height:
            show(cs.rename({"column": "Column", "type": "Type", "records_differing": "Records differing",
                            "pct_of_paired": "% of paired", "example (sqlserver vs postgres)": "Example (SQL Server vs Postgres)"}))
        else:
            st.success("No column differs in any paired record.")

        # records explorer
        st.subheader("Records")
        recs = res.records
        present = [x for x in RESULTS if recs.filter(pl.col("result") == x).height]
        default = [x for x in present if x != IDENTICAL] or present
        fc = st.columns([2, 2, 1])
        want = fc[0].multiselect("Result", RESULTS, default=default, format_func=lambda x: RESULT_ICON[x],
                                 key=f"res_{job}_{name}")
        diff_cols = ["(any)"] + cs["column"].to_list()
        only_col = fc[1].selectbox("Differing in column", diff_cols, key=f"col_{job}_{name}")
        wide = fc[2].toggle("All columns", key=f"wide_{job}_{name}",
                            help="Show every column side by side (<col>_sql, <col>_pg, <col>_ok)")
        rview = recs.filter(pl.col("result").is_in(want))
        if only_col != "(any)":
            rview = rview.filter(pl.col("diff_columns").str.split(", ").list.contains(only_col))
        rview = rview.with_columns(pl.col("result").replace_strict(RESULT_ICON, default=pl.col("result")))
        lead = (["machine"] + [k for k in spec.key[1:]] + [OCC, "result", "diff_columns", "known_reason",
                                                          "sqlserver_source_id", "postgres_source_id"])
        shown = rview if wide else rview.select([c for c in lead if c in rview.columns])
        st.caption(f"{rview.height:,} of {recs.height:,} records. **Click a record** to compare it column by column.")
        ev = show(shown, on_select="rerun", selection_mode="single-row", key=f"recs_{job}_{name}")
        picked_rows = ev.selection.rows if ev and ev.selection else []
        if picked_rows:
            rec = rview.row(picked_rows[0], named=True)
            raw_result = {v: k for k, v in RESULT_ICON.items()}[rec["result"]]
            st.markdown(f"**Record detail** - {RESULT_ICON[raw_result]}"
                        + (f" · known gap: {rec['known_reason']}" if rec.get("known_reason") else "")
                        + f" · SQL Server id `{rec['sqlserver_source_id']}` · Postgres id `{rec['postgres_source_id']}`")
            drows = []
            for c in spec.columns:
                sv, pv = rec.get(f"{c}_sql"), rec.get(f"{c}_pg")
                if c not in spec.compare_columns:
                    status = "– shown only"
                elif raw_result == ONLY_SQL:
                    status = "🟠 no Postgres record"
                elif raw_result == ONLY_PG:
                    status = "🟣 no SQL Server record"
                else:
                    status = "✅ same" if rec.get(f"{c}_ok") else "❌ differs"
                drows.append({"Column": c, "SQL Server": sv, "Postgres": pv, "Status": status,
                              "SQL Server expression": spec.columns[c].sql, "Postgres expression": spec.columns[c].pg})
            detail = pl.DataFrame(drows, schema={"Column": pl.String, "SQL Server": pl.String, "Postgres": pl.String,
                                                 "Status": pl.String, "SQL Server expression": pl.String,
                                                 "Postgres expression": pl.String})
            only_diff = st.toggle("Only differing columns", key=f"od_{job}_{name}")
            show(detail.filter(pl.col("Status") == "❌ differs") if only_diff else detail)
            info = {k: v for k, v in rec.items() if k.startswith(("sql_info_", "pg_info_"))}
            if info:
                with st.expander("Extra information (not compared)"):
                    show(pl.DataFrame({"Field": [k.replace("sql_info_", "SQL Server: ").replace("pg_info_", "Postgres: ")
                                                 for k in info],
                                       "Value": [None if v is None else str(v) for v in info.values()]}))

        with st.expander(f"Every differing value ({res.mismatches.height:,})"):
            if res.mismatches.height:
                show(res.mismatches)
            else:
                st.caption("None.")

        st.subheader("Invalid records")
        if met.invalid.height:
            st.caption("rawdata: a status other than processed / not processed. Aggregated and autodata tables: a "
                       "code that does not resolve in that side's master data.")
            show(met.invalid)
        else:
            st.success("No invalid records on either side.")

        st.subheader("Known gaps")
        if res.known_summary.height:
            show(res.known_summary.with_columns(pl.col("side").replace_strict(
                {ONLY_SQL: "SQL Server", ONLY_PG: "Postgres"}, default=pl.col("side"))))
        else:
            st.caption("No known-difference rules are configured for this table.")

        if spec.not_compared:
            st.subheader("Columns not compared")
            show(pl.DataFrame({"Column": list(spec.not_compared),
                               "Reason": [w or "" for w in spec.not_compared.values()]}))

# ---------------- run info & mapping
if view == "Run info & mapping":
    w = r.window
    info_rows = [("Machine", r.machine_label),
                 ("Logical day", f"{w.start:%Y-%m-%d %H:%M:%S} (included) to {w.end:%Y-%m-%d %H:%M:%S} (excluded)"),
                 ("SQL Server", r.servers.get("sqlserver", "")), ("Postgres", r.servers.get("postgres", "")),
                 ("Compare mode", "strict (values exactly as stored)" if r.strict
                  else "lenient (trimmed, blank = NULL, leading zeros in ids ignored)"),
                 ("Run at", f"{r.run_at:%Y-%m-%d %H:%M:%S} ({r.seconds:,.1f} s)")]
    for name, t in r.tables.items():
        spec = t.result.spec
        if "datatype" in spec.columns:
            info_rows.append((f"{name} datatypes", ", ".join(map(str, t.datatypes)) or "all"))
        rule = pipeline.window_rule(spec)
        info_rows += [(f"{name} records selected (SQL Server)", rule["sqlserver"]),
                      (f"{name} records selected (Postgres)", rule["postgres"]),
                      (f"{name} time precision", "SQL Server datetime (1/300 s), both sides"
                       if spec.datetime_precision == "sqlserver_datetime" else "exact (microseconds)"),
                      (f"{name} pairing key", " + ".join(spec.key))]
    show(pl.DataFrame(info_rows, schema=["Item", "Value"], orient="row"))

    st.subheader("Column mapping")
    for name, t in r.tables.items():
        spec = t.result.spec
        with st.expander(f"{name}: {spec.label.split('-', 1)[-1].strip()}"):
            cmap = [{"Column": c.name, "Type": c.type, "Compared": "yes" if c.compare else "shown only",
                     "Pairing key": "yes" if c.name in spec.key else "", "SQL Server": c.sql, "Postgres": c.pg,
                     "Note": ""} for c in spec.columns.values()]
            cmap += [{"Column": c, "Type": "", "Compared": "no", "Pairing key": "", "SQL Server": "",
                      "Postgres": "", "Note": why} for c, why in spec.not_compared.items()]
            show(pl.DataFrame(cmap))
