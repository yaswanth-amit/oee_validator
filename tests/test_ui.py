"""Screen tests for ui.py with Streamlit's AppTest. No database: the machine masters and the
run are replaced by synthetic data that goes through the real comparison engine.

    python -m pytest oee_validator/tests -q
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from oee_validator.core import pipeline  # noqa: E402
from oee_validator.core.db import Window  # noqa: E402
from oee_validator.core.machines import machines_frame  # noqa: E402

from .test_validator import (ALIASES, AUTO, MACHINES, RAW, auto_row, pg_auto, run_auto,  # noqa: E402
                             shift_pg, shift_row, SHIFT)

UI = str(Path(__file__).resolve().parent.parent / "ui.py")
SQL_M = {"interface_id": ["1001", "1002", "1009"], "name": ["Machine-01", "Machine-02", "Old-01"]}
PG_M = {"interface_id": ["1001", "1002"], "iot_id": ["1", "3"], "name": ["Machine-01", "Machine-02"]}


def test_master_list_marks_machines_that_cannot_be_compared():
    df = pipeline.master_list(SQL_M, PG_M)
    assert df.filter(pl.col("comparable"))["machine_name"].to_list() == ["Machine-01", "Machine-02"]
    assert df.filter(pl.col("machine_name") == "Old-01")["note"][0] == "only in SQL Server"


def test_machines_frame_checks_like_the_csv():
    df = machines_frame([("Machine-01", datetime(2026, 10, 1, 6), datetime(2026, 10, 2, 6))])
    assert df["machine_name"].to_list() == ["Machine-01"] and df["logical_day_start"].dtype == pl.Datetime("us")
    with pytest.raises(Exception, match="more than once"):
        machines_frame([("M", datetime(2026, 10, 1, 6), datetime(2026, 10, 2, 6))] * 2)


def fake_run(machines, tables=None, **kw):
    """One result per selected row: Machine-01 differs in autodata, everything else matches."""
    for i in range(machines.height):
        row = machines.row(i, named=True)
        job = MACHINES.filter(pl.col("machine_name") == row["machine_name"]).with_columns(
            pl.lit(0, dtype=pl.UInt32).alias("machine_order"))
        r = pipeline.RunResult(window=Window(row["logical_day_start"], row["logical_day_end"]), machines=job,
                               strict=True, run_at=datetime(2026, 10, 2, 7), servers={"sqlserver": "S", "postgres": "P"},
                               job=i + 1)
        mid = job["sqlserver_interface_id"][0]
        a = [auto_row(1, machine=mid), auto_row(2, machine=mid)]
        b = [pg_auto(a[0]), pg_auto(dict(a[1], cycle_time=51 if row["machine_name"] == "Machine-01" else 50))]
        for name in tables or ["autodata", "shift_production"]:
            if name == "autodata":
                r.tables[name] = run_auto(a, b, m=job)
            else:
                s = [shift_row(1, machine=row["machine_name"])]
                r.tables[name] = run_auto(s, [shift_pg(x) for x in s], spec=SHIFT, m=job)
        r.findings = pipeline.findings(r)
        yield r


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(pipeline, "master_machines", lambda conn_cfg=None: (
        {"sqlserver": "SQL-QA / DB", "postgres": "pg:5434 / db"}, pipeline.master_list(SQL_M, PG_M)))
    monkeypatch.setattr(pipeline, "run", fake_run)
    at = AppTest.from_file(UI, default_timeout=60)
    at.run()
    assert not at.exception
    return at


def test_start_screen_lists_comparable_machines(app):
    ms = app.sidebar.multiselect[0]
    assert ms.label == "Machines" and ms.options == ["Machine-01", "Machine-02"]
    assert any("cannot be compared" in e.label for e in app.sidebar.expander)
    assert "Run comparison" in [b.label for b in app.sidebar.button]


def test_run_shows_overview_details_and_record_detail(app):
    app.sidebar.multiselect[0].set_value(["Machine-01", "Machine-02"])
    app.sidebar.date_input[0].set_value(date(2026, 10, 1))        # From 2026-10-01 06:00
    app.sidebar.date_input[1].set_value(date(2026, 10, 3))        # To   2026-10-03 06:00 -> 2 days
    tables = next(m for m in app.sidebar.multiselect if m.label == "Tables to compare")
    tables.set_value(["autodata", "shift_production"])
    app.run()
    next(b for b in app.sidebar.button if b.label == "Run comparison").click()
    app.run()
    assert not app.exception, app.exception
    metrics = {m.label: m.value for m in app.metric[:4]}
    assert metrics == {"Comparisons": "4", "Passed": "2", "Failed": "2", "Errors": "0"}
    assert any("FAIL" in e.value for e in app.error)                      # first job = Machine-01, day 1
    subheaders = [s.value for s in app.subheader]
    for s in ("Overview", "Findings", "Verdict per table", "Data coverage"):
        assert s in subheaders, s
    app.session_state["view_0"] = "autodata"                      # the view selector keeps its choice
    app.run()
    assert not app.exception, app.exception
    subheaders = [s.value for s in app.subheader]
    for s in ("Columns with differences", "Records", "Invalid records", "Known gaps", "Columns not compared"):
        assert s in subheaders, s
    app.session_state["view_0"] = "Run info & mapping"
    app.run()
    assert "Column mapping" in [s.value for s in app.subheader] and not app.exception
    labels = [d.label for d in app.get("download_button")]
    assert "Download Markdown report" in labels and "Download Excel report" in labels
    assert not app.get("help"), "a bare expression was rendered by Streamlit magic"


def test_from_to_window_one_comparison_when_not_split(app):
    app.sidebar.multiselect[0].set_value(["Machine-01"])
    app.sidebar.date_input[0].set_value(date(2026, 10, 1))
    app.sidebar.date_input[1].set_value(date(2026, 10, 3))
    next(c for c in app.sidebar.checkbox if c.label == "One comparison per day").uncheck()
    app.run()
    assert any("1 window(s) = **1 comparison(s)**" in c.value for c in app.sidebar.caption)
    app.sidebar.date_input[1].set_value(date(2026, 9, 30))           # To before From
    app.run()
    assert any("To must be after From" in e.value for e in app.sidebar.error)
    assert next(b for b in app.sidebar.button if b.label == "Run comparison").disabled
