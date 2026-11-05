"""Engine tests on synthetic driver-style data. No database needed.

    python -m pytest oee_validator/tests -q
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import openpyxl
import polars as pl
import pytest

from oee_validator.core.compare import (DIFFERENT, FAIL, IDENTICAL, NO_DATA, ONLY_PG, ONLY_SQL, PASS,
                                        PASS_KNOWN)
from oee_validator.core.config import ConfigError, load_aliases, load_table_specs, parse_table
from oee_validator.core.db import SOURCE_ID, Window, build_count, build_select
from oee_validator.core.excel import write_workbook
from oee_validator.core.machines import MachineListError, check_against_masters, load_machines
from oee_validator.core.markdown import render
from oee_validator.core.normalize import canon_decimal, canon_int, canonical_series, sqlserver_datetime_round
from oee_validator.core.pipeline import RunResult, compare_table, findings

SPECS = load_table_specs()
AUTO, RAW = SPECS["autodata"], SPECS["rawdata"]
ALIASES = load_aliases()
T0 = datetime(2026, 10, 1, 8, 0, 0)
WINDOW = Window(datetime(2026, 10, 1), datetime(2026, 10, 2))


def machines(rows=(("Machine-01", "1001", "1001", "1"), ("Machine-02", "1002", "1002", "3"))) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=["machine_name", "sqlserver_interface_id", "postgres_interface_id",
                                      "postgres_iot_id"], orient="row").with_row_index("machine_order")


MACHINES = machines()


def auto_row(i: int, **kw) -> dict:
    st = kw.pop("st_time", T0.replace(minute=i % 60, hour=8 + i // 60))
    r = {SOURCE_ID: i, "machine": "1001", "component": "C1", "operation": "10", "operator": "7",
         "datatype": 1, "down_code": None, "st_date": date(2026, 10, 1), "st_time": st,
         "nd_date": date(2026, 10, 1), "nd_time": st.replace(second=50), "mst_time": st,
         "cycle_time": 50, "load_unload": 5, "part_count": Decimal("1.0000"), "comp_slno": 0,
         "work_order_number": "WO1", "serial_number": None, "heat_code": None}
    r.update(kw)
    return r


def pg_auto(row: dict, **info) -> dict:
    r = dict(row)
    r["st_date"] = row["st_date"]
    r.setdefault("info__is_icd", False)
    for k in ("machine_iot_id", "component_iot_id", "operation_iot_id", "operator_iot_id", "down_iot_id"):
        r.setdefault(f"info__{k}", "1")
    # by default the iot id links to the master with the same interface id
    r.setdefault("info__master_component_interface_id", row["component"])
    r.setdefault("info__master_operation_interface_id", row["operation"])
    for k, v in info.items():
        r[f"info__{k}"] = v
    return r


def to_raw(rows: list[dict], spec=AUTO, side="sqlserver") -> dict[str, list]:
    cols = [SOURCE_ID] + list(spec.columns)
    if rows:
        cols += [c for c in rows[0] if c.startswith("info__")]
    return {c: [r.get(c) for r in rows] for c in cols}


def run_auto(sql_rows, pg_rows, strict=True, masters=None, spec=AUTO, m=MACHINES):
    if spec.name == "autodata":
        sql_rows = [{"info__is_icd": 0, **r} for r in sql_rows]
    return compare_table(spec, to_raw(sql_rows, spec), to_raw(pg_rows, spec, "postgres"), m, ALIASES,
                         strict=strict, sql_masters=masters)


def verdict(tr, machine="Machine-01"):
    return tr.result.machine_summary.filter(pl.col("machine") == machine)["verdict"][0]


# --------------------------------------------------------------------------- comparison

def test_identical_records_pass():
    rows = [auto_row(i) for i in range(5)]
    tr = run_auto(rows, [pg_auto(r) for r in rows])
    t = tr.result.totals
    assert t[IDENTICAL] == 5 and t["unexplained"] == 0
    assert verdict(tr) == PASS
    assert verdict(tr, "Machine-02") == NO_DATA


def test_decimal_trailing_zeros_equal_but_real_digits_differ():
    s = [auto_row(1, part_count=Decimal("1.50000")), auto_row(2, part_count=Decimal("2"))]
    p = [pg_auto(auto_row(1, part_count=Decimal("1.5"))), pg_auto(auto_row(2, part_count=Decimal("2.00001")))]
    tr = run_auto(s, p)
    assert tr.result.totals[IDENTICAL] == 1 and tr.result.totals[DIFFERENT] == 1
    mm = tr.result.mismatches.row(0, named=True)
    assert (mm["column"], mm["sqlserver_value"], mm["postgres_value"]) == ("part_count", "2", "2.00001")


def test_sqlserver_datetime_precision_pairs_rounded_timestamps():
    st_sql = T0.replace(microsecond=3000)        # SQL Server shows .003
    st_pg = T0.replace(microsecond=4000)         # same input string, Postgres keeps .004
    s = [auto_row(1, st_time=st_sql, mst_time=st_sql, nd_time=st_sql.replace(second=50))]
    p = [pg_auto(auto_row(1, st_time=st_pg, mst_time=st_pg, nd_time=st_pg.replace(second=50)))]
    assert run_auto(s, p).result.totals[IDENTICAL] == 1
    import dataclasses
    exact = dataclasses.replace(AUTO, datetime_precision="exact")
    t = run_auto(s, p, spec=exact).result.totals
    assert t[ONLY_SQL] == 1 and t[ONLY_PG] == 1


def test_polars_and_python_rounding_agree():
    from oee_validator.core.config import ColumnSpec
    col = ColumnSpec("t", "datetime", "", "")
    vals = [T0.replace(microsecond=us) for us in (0, 1665, 1667, 3000, 3333, 4000, 5000, 6667, 998_334, 999_999)]
    fast = canonical_series(vals, col, "postgres", ALIASES, True, "sqlserver_datetime").to_list()
    slow = [sqlserver_datetime_round(v).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] for v in vals]
    assert fast == slow
    assert fast[3] == "2026-10-01 08:00:00.003" and fast[-1] == "2026-10-01 08:00:01.000"


def test_predefined_downcodes_match_by_name():
    s = [auto_row(1, datatype=2, down_code="NO_DATA"), auto_row(2, datatype=2, down_code="McTI"),
         auto_row(3, datatype=2, down_code="UNKNOWN")]
    p = [pg_auto(auto_row(1, datatype=2, down_code="no_data")), pg_auto(auto_row(2, datatype=2, down_code="mcti")),
         pg_auto(auto_row(3, datatype=2, down_code="unknown"))]
    assert run_auto(s, p).result.totals[IDENTICAL] == 3


def test_real_difference_is_reported_with_column_and_example():
    s = [auto_row(1, datatype=2, down_code="1003")]
    p = [pg_auto(auto_row(1, datatype=2, down_code="1004"))]
    tr = run_auto(s, p)
    assert verdict(tr) == FAIL
    cs = tr.result.column_summary.row(0, named=True)
    assert cs["column"] == "down_code" and cs["records_differing"] == 1
    rec = tr.result.records.row(0, named=True)
    assert rec["result"] == DIFFERENT and rec["diff_columns"] == "down_code" and rec["down_code_ok"] is False


def test_known_gap_does_not_fail_the_machine():
    s = [auto_row(1), auto_row(2, datatype=2, down_code="NPCy")]
    p = [pg_auto(auto_row(1))]
    tr = run_auto(s, p)
    assert tr.result.totals[ONLY_SQL] == 1 and tr.result.totals["only_sqlserver_known"] == 1
    assert tr.result.totals["unexplained"] == 0 and tr.result.passed
    assert verdict(tr) == PASS_KNOWN
    ks = tr.result.known_summary.filter(pl.col("records") > 0)
    assert ks.height == 1 and "NPCy" in ks["reason"][0]


def test_unexplained_one_sided_record_fails():
    rows = [auto_row(1)]
    tr = run_auto(rows, [pg_auto(rows[0]), pg_auto(auto_row(2))])
    assert tr.result.totals[ONLY_PG] == 1 and verdict(tr) == FAIL and not tr.result.passed


def test_repeated_key_pairs_in_source_id_order():
    s = [auto_row(10, cycle_time=40, st_time=T0), auto_row(11, cycle_time=41, st_time=T0)]
    p = [pg_auto(auto_row(20, cycle_time=40, st_time=T0)), pg_auto(auto_row(21, cycle_time=41, st_time=T0))]
    tr = run_auto(s, p)
    assert tr.result.totals[IDENTICAL] == 2
    assert tr.result.records["occurrence"].to_list() == [0, 1]


def test_strict_vs_lenient():
    s = [auto_row(1, operator=" 7", heat_code="")]
    p = [pg_auto(auto_row(1, operator="0007", heat_code=None))]
    strict = run_auto(s, p)
    assert strict.result.totals["paired"] == 1          # still paired via the key
    assert set(strict.result.mismatches["column"]) == {"operator", "heat_code"}
    assert run_auto(s, p, strict=False).result.passed


def test_rawdata_status_compares_by_meaning_and_start_not_before_end_is_known():
    def raw(i, status, dt=1, machine="1001"):
        st = T0.replace(minute=i)
        return {SOURCE_ID: i, "machine": machine, "component": "C1", "operation": "10", "operator": "7",
                "datatype": dt, "st_time": st, "nd_time": st, "down_code": None,
                "part_count": 1 if dt == 1 else None, "work_order_number": None, "serial_number": None,
                "heat_code": None, "supervisor_code": None, "status": status}
    s = [raw(1, 7), raw(2, 1), raw(3, 8), raw(4, 15, dt=11), raw(5, 1, dt=22)]
    p = [raw(1, 8), raw(2, 1), raw(4, 0, dt=11)]
    tr = compare_table(RAW, to_raw(s, RAW), to_raw(p, RAW), MACHINES, ALIASES)
    t = tr.result.totals
    assert t[IDENTICAL] == 3 and t[ONLY_SQL] == 2 and t["only_sqlserver_known"] == 2 and tr.result.passed
    counts = tr.metrics.counts.filter(pl.col("machine") == "Machine-01")
    get = {r["metric"]: (r["sqlserver"], r["postgres"]) for r in counts.iter_rows(named=True)}
    assert get["Total records"] == ("5", "3")
    assert get["Not processed"] == ("1", "1")
    assert get["Invalid records"] == ("2", "1")         # future_dated both sides + start_not_before_end


def test_machine_pairs_by_name_when_ids_differ():
    m = machines((("Press-7", "1001", "P-7", "9"),))
    rows = [auto_row(1)]
    pg = [pg_auto(auto_row(1, machine="P-7"))]
    tr = run_auto(rows, pg, m=m)
    rec = tr.result.records.row(0, named=True)
    assert rec["machine"] == "Press-7" and rec["result"] == IDENTICAL
    assert (rec["sql_info_machine_id"], rec["pg_info_machine_id"]) == ("1001", "P-7")


# --------------------------------------------------------------------------- metrics

def test_invalid_and_totals():
    s = [auto_row(1, component="GONE"), auto_row(2, datatype=2, down_code="D1", cycle_time=0,
                                                   nd_time=T0.replace(minute=2, second=30))]
    p = [pg_auto(auto_row(1, component=None), component_iot_id="77"),
         pg_auto(auto_row(2, datatype=2, down_code="D1", cycle_time=0, nd_time=T0.replace(minute=2, second=30)))]
    masters = {"component": {"c1"}, "operation": {"10"}, "operator": {"7"}, "down_code": {"d1"}}
    tr = run_auto(s, p, masters=masters)
    inv = tr.metrics.invalid
    assert inv.filter(pl.col("side") == "sqlserver")["reason"].to_list() == [
        "component not in SQL Server master (dropped by the shift aggregation)"]
    assert inv.filter(pl.col("side") == "postgres")["reason"].to_list() == [
        "component iot id 77 does not resolve in master_data"]
    get = {r["metric"]: (r["sqlserver"], r["postgres"])
           for r in tr.metrics.counts.filter(pl.col("machine") == "Machine-01").iter_rows(named=True)}
    assert get["Sum cycle_time, cycles (s)"] == ("50", "50")
    assert get["Sum down duration (s)"] == ("30", "30")
    assert get["ICD (in-cycle downs)"] == ("0", "0")
    f = findings(_run_result({"autodata": tr}))
    assert any("iot id (77) that is missing from master_data" in x for x in f)


def test_icd_counted_on_both_sides():
    down = dict(datatype=2, down_code="D1", cycle_time=0, nd_time=T0.replace(minute=2, second=30))
    s = [auto_row(1), {**auto_row(2, **down), "info__is_icd": 1}]
    p = [pg_auto(auto_row(1)), pg_auto(auto_row(2, **down), is_icd=True)]
    tr = run_auto(s, p)
    get = {r["metric"]: (r["sqlserver"], r["postgres"])
           for r in tr.metrics.counts.filter(pl.col("machine") == "Machine-01").iter_rows(named=True)}
    assert get["ICD (in-cycle downs)"] == ("1", "1")


def test_one_side_flag_form_still_accepted():
    from oee_validator.core.config import _flag_sides
    assert _flag_sides({"side": "postgres", "info": "is_icd", "value": "True"}) == {
        "postgres": {"info": "is_icd", "value": "True"}}


def test_component_without_master_compares_stored_id_and_is_flagged():
    """Raw data arrives before the component master exists: Postgres keeps the interface id
    in component_interface_id and leaves the iot id empty until it is linked later."""
    s = [auto_row(1, component="NEWC")]
    p = [pg_auto(auto_row(1, component="NEWC"), component_iot_id=None, master_component_interface_id=None)]
    tr = run_auto(s, p, masters={"component": {"c1"}, "operation": {"10"}, "operator": {"7"}})
    assert tr.result.totals[IDENTICAL] == 1                 # same interface id on both sides
    inv = tr.metrics.invalid
    assert inv.filter(pl.col("side") == "postgres")["reason"].to_list() == [
        "component 'NEWC' has no Postgres master yet (iot id empty)"]
    assert inv.filter(pl.col("side") == "sqlserver")["reason"].to_list() == [
        "component not in SQL Server master (dropped by the shift aggregation)"]


def test_iot_id_linked_to_wrong_master_is_flagged():
    """The record carries 'Pra' but its iot id points to the master 'PraJ1'."""
    s = [auto_row(1, component="Pra")]
    p = [pg_auto(auto_row(1, component="Pra"), component_iot_id="114", master_component_interface_id="PraJ1")]
    tr = run_auto(s, p)
    assert tr.result.totals[IDENTICAL] == 1
    assert tr.metrics.invalid["reason"].to_list() == [
        "component iot id 114 is linked to master 'PraJ1' but the record carries 'Pra'"]


def test_correctly_linked_component_is_not_invalid():
    rows = [auto_row(1)]
    tr = run_auto(rows, [pg_auto(rows[0], component_iot_id="5")])
    assert tr.metrics.invalid.height == 0


def test_coverage_reports_missing_side():
    tr = run_auto([auto_row(1)], [])
    cov = tr.metrics.overlap.filter(pl.col("machine") == "Machine-01").row(0, named=True)
    assert cov["has_overlap"] is False
    f = findings(_run_result({"autodata": tr}))
    assert any("none in Postgres" in x for x in f)


# --------------------------------------------------------------------------- normalisation

@pytest.mark.parametrize("v, expected", [
    (Decimal("1.50000"), "1.5"), (Decimal("-0.000"), "0"), (1.1, "1.1"), (100, "100"),
    (Decimal("1E+2"), "100"), ("abc", "<not a number: 'abc'>"), (None, None)])
def test_canon_decimal(v, expected):
    assert canon_decimal(v) == expected


def test_canon_int_rejects_fractions():
    assert canon_int(Decimal("5.000")) == "5"
    assert canon_int(Decimal("5.5")).startswith("<not an integer")


def test_int_column_with_mixed_driver_types():
    from oee_validator.core.config import ColumnSpec
    col = ColumnSpec("x", "int", "", "")
    assert canonical_series([1, Decimal("2"), None, "3"], col, "sqlserver", ALIASES, True).to_list() == \
        ["1", "2", None, "3"]


# --------------------------------------------------------------------------- machines, config, queries

CSV_HEAD = ("machine_name,sqlserver_interface_id,postgres_interface_id,postgres_iot_id,"
            "logical_day_start,logical_day_end\n")
DAY1 = "2026-10-10 06:00:00,2026-10-11 06:00:00"
DAY2 = "2026-10-11 06:00:00,2026-10-12 06:00:00"


def test_machine_csv_checks(tmp_path):
    f = tmp_path / "m.csv"
    f.write_text(CSV_HEAD + f"A,1,1,1,{DAY1}\nA,2,2,2,{DAY2}\nB,,3,3,{DAY1}\n", encoding="utf-8")
    with pytest.raises(MachineListError) as e:
        load_machines(f)
    assert any("machine_name with different ids on different rows: A" in p for p in e.value.problems)
    assert any("'sqlserver_interface_id' is empty" in p for p in e.value.problems)
    f.write_text("machine_name,sqlserver_interface_id\nA,1\n", encoding="utf-8")
    with pytest.raises(MachineListError, match="missing column"):
        load_machines(f)
    # the old 4-column file is rejected with the names of the new columns
    f.write_text("machine_name,sqlserver_interface_id,postgres_interface_id,postgres_iot_id\nA,1,1,1\n",
                 encoding="utf-8")
    with pytest.raises(MachineListError, match="logical_day_start, logical_day_end"):
        load_machines(f)


def test_machine_csv_one_job_per_row(tmp_path):
    f = tmp_path / "m.csv"
    f.write_text(CSV_HEAD + f"A,1,1,1,{DAY1}\nA,1,1,1,{DAY2}\nB,2,2,2,2026-10-10 06:00,2026-10-11 06:00\n",
                 encoding="utf-8")
    df = load_machines(f)
    assert df.height == 3 and df["machine_name"].to_list() == ["A", "A", "B"]
    assert df["logical_day_start"].to_list() == [datetime(2026, 10, 10, 6), datetime(2026, 10, 11, 6),
                                                 datetime(2026, 10, 10, 6)]
    assert df["logical_day_end"][0] == datetime(2026, 10, 11, 6)


@pytest.mark.parametrize("rows, message", [
    (f"A,1,1,1,{DAY1}\nA,1,1,1,{DAY1}\n", "the same machine and logical day appear more than once"),
    (f"A,1,1,1,{DAY1}\nB,1,2,2,{DAY1}\n", "sqlserver_interface_id used by more than one machine: 1"),
    ("A,1,1,1,10-10-2026 06:00,2026-10-11 06:00:00\n", "is not a timestamp"),
    ("A,1,1,1,2026-10-11 06:00:00,2026-10-10 06:00:00\n", "is not after logical_day_start"),
    ("A,1,1,1,2026-10-10 06:00:00,\n", "'logical_day_end' is empty"),
])
def test_machine_csv_rejects_bad_rows(tmp_path, rows, message):
    f = tmp_path / "m.csv"
    f.write_text(CSV_HEAD + rows, encoding="utf-8")
    with pytest.raises(MachineListError, match=message):
        load_machines(f)


def test_machine_master_check_catches_wrong_iot_id():
    sql = {"interface_id": ["1001"], "name": ["M1"]}
    pg = {"interface_id": ["1001", "1002"], "iot_id": ["1", "3"], "name": ["M1", "M2"]}
    ok = check_against_masters(machines((("M1", "1001", "1001", "1"),)), sql, pg)
    assert ok["found_in_sqlserver"].to_list() == [True]
    with pytest.raises(MachineListError, match="belongs to interface id '1002'"):
        check_against_masters(machines((("M1", "1001", "1001", "3"),)), sql, pg)


def test_query_filters_machines_window_and_datatypes():
    sql, params = build_select(AUTO, "postgres", ["1", "3"], WINDOW, [1, 2], ALIASES)
    assert "(a.machine_iot_id) IN (%s, %s)" in sql
    assert "tsrange(%s::timestamp, %s::timestamp, '[)') && a.cycletime_range" in sql
    assert "a.st_time) >=" not in sql                      # autodata uses the cycle range, not st_time
    S, E = WINDOW.start, WINDOW.end
    assert params == ["1", "3", S, E, S, E, 1, 2]          # overlap (S, E), then zero-length rows (S, E)
    assert "'no_data'" in sql                              # predefined names injected, quoted
    sql, params = build_count(RAW, "sqlserver", ["1001"], WINDOW, [], ALIASES)
    assert "(r.Mc) IN (?)" in sql and "(r.Sttime) >= ?" in sql and "(r.Sttime) < ?" in sql
    assert "DataType" not in sql and params == ["1001", WINDOW.start, WINDOW.end]


def test_sqlserver_autodata_window_is_sqlservers_four_cases():
    sql, params = build_count(AUTO, "sqlserver", ["1001"], WINDOW, [1, 2], ALIASES)
    flat = " ".join(sql.split())
    for case in ("(a.msttime >= ? AND a.ndtime <= ?)",
                 "(a.msttime < ? AND a.ndtime > ? AND a.ndtime <= ?)",
                 "(a.msttime >= ? AND a.msttime < ? AND a.ndtime > ?)",
                 "(a.msttime < ? AND a.ndtime > ?)"):
        assert case in flat
    assert "a.msttime IS NOT NULL AND a.ndtime IS NOT NULL" in flat
    assert "{start}" not in sql and "{end}" not in sql
    S, E = WINDOW.start, WINDOW.end                        # bound in the order the placeholders appear
    assert params == ["1001", S, E, S, S, E, S, E, E, S, E, 1, 2]


def _eval_window_rule(side: str, m, n, S, E) -> bool:
    """Evaluate the real window_filter text of autodata.yaml in Python (m = mst_time, n = nd_time).

    The Postgres range overlap tsrange(S, E, '[)') && tsrange(m, n, '[)') is replaced by its
    definition for non-null bounds: m < E and n > S, and an empty range (m = n) overlaps nothing.
    Any text this translation does not know makes the test fail, so a changed rule is noticed.
    """
    t = " ".join(AUTO.window_filter[side].split())
    if side == "postgres":
        t = t.replace("tsrange({start}::timestamp, {end}::timestamp, '[)') && a.cycletime_range",
                      "(m < E and n > S and m < n)")
        t = t.replace("a.mst_time = a.nd_time", "m == n").replace("a.mst_time", "m").replace("a.nd_time", "n")
    else:
        t = t.replace("a.msttime", "m").replace("a.ndtime", "n")
    t = (t.replace("{start}", "S").replace("{end}", "E").replace(" IS NOT NULL", " is not None")
          .replace(" AND ", " and ").replace(" OR ", " or "))
    assert "a." not in t and "{" not in t and "tsrange" not in t, t
    return bool(eval(t, {"__builtins__": {}}, {"m": m, "n": n, "S": S, "E": E}))  # noqa: S307 - test-only, own config text


_S, _E = datetime(2026, 10, 10, 6), datetime(2026, 10, 11, 6)


@pytest.mark.parametrize("m, n, expected", [
    (datetime(2026, 10, 10, 5), datetime(2026, 10, 10, 6), False),        # ends exactly at the start
    (datetime(2026, 10, 10, 5), datetime(2026, 10, 10, 6, 0, 1), True),   # crosses the start (case 2)
    (datetime(2026, 10, 10, 6), datetime(2026, 10, 10, 6, 1), True),      # starts at the start (case 1)
    (datetime(2026, 10, 10, 9), datetime(2026, 10, 10, 9, 1), True),      # fully inside (case 1)
    (datetime(2026, 10, 11, 5, 59), datetime(2026, 10, 11, 6, 5), True),  # crosses the end (case 3)
    (datetime(2026, 10, 11, 6), datetime(2026, 10, 11, 6, 5), False),     # starts at the end
    (datetime(2026, 10, 10, 5), datetime(2026, 10, 11, 7), True),         # spans the whole day (case 4)
    (datetime(2026, 10, 10, 8), datetime(2026, 10, 10, 8), True),         # zero-length inside (case 1)
    (_S, _S, True),                                                       # zero-length at the start
    (_E, _E, True),                                                       # zero-length at the end (case 1: <= E)
    (datetime(2026, 10, 10, 5), datetime(2026, 10, 10, 5), False),        # zero-length before the day
    (None, datetime(2026, 10, 10, 8), False), (datetime(2026, 10, 10, 8), None, False), (None, None, False),
])
def test_both_sides_select_sqlservers_four_cases(m, n, expected):
    assert _eval_window_rule("sqlserver", m, n, _S, _E) is expected
    assert _eval_window_rule("postgres", m, n, _S, _E) is expected


def test_inverted_sqlserver_row_inside_the_day_is_selected():
    """msttime > ndtime cannot exist in Postgres (tsrange rejects it); SQL Server's case 1 still
    selects it, so it is reported as only_sqlserver instead of silently disappearing."""
    assert _eval_window_rule("sqlserver", datetime(2026, 10, 10, 9), datetime(2026, 10, 10, 8), _S, _E) is True


def test_config_window_filter_needs_both_placeholders():
    raw = {"name": "x", "columns": {"machine": {"type": "str", "sql": "a", "pg": "b"},
                                    "datatype": {"type": "int", "sql": "a", "pg": "b"},
                                    "st_time": {"type": "datetime", "sql": "a", "pg": "b"}},
           "key": ["machine"], "sqlserver": {"from": "t", "source_id": "id"},
           "postgres": {"from": "t", "source_id": "id"},
           "machine_filter": {s: {"expr": "m", "csv": "sqlserver_interface_id"} for s in ("sqlserver", "postgres")},
           "machine_key": {"sqlserver": "sqlserver_interface_id", "postgres": "postgres_interface_id"},
           "window_filter": {"sqlserver": "t < {end}", "postgres": "r && tsrange({start}, {end})"}}
    with pytest.raises(ConfigError, match="window_filter.sqlserver must use both"):
        parse_table(raw)
    raw["window_filter"]["sqlserver"] = "t >= {start} AND t < {end}"
    assert parse_table(raw).window_filter["sqlserver"] == "t >= {start} AND t < {end}"


def test_config_rejects_bad_key():
    raw = {"name": "x", "columns": {"machine": {"type": "str", "sql": "a", "pg": "b"},
                                    "datatype": {"type": "int", "sql": "a", "pg": "b"}},
           "key": ["datatype"], "sqlserver": {"from": "t", "source_id": "id"},
           "postgres": {"from": "t", "source_id": "id"}}
    with pytest.raises(ConfigError, match="must start with 'machine'"):
        parse_table(raw)


# --------------------------------------------------------------------------- outputs

def _run_result(tables) -> RunResult:
    r = RunResult(window=WINDOW, machines=MACHINES.with_columns(pl.lit(True).alias("found_in_sqlserver")),
                  strict=True, run_at=datetime(2026, 10, 2, 9, 0), servers={"sqlserver": "S / db", "postgres": "P / db"},
                  tables=tables)
    r.findings = findings(r)
    return r


def test_markdown_and_workbook(tmp_path):
    s = [auto_row(1), auto_row(2, datatype=2, down_code="1003"), auto_row(3, datatype=2, down_code="NPCy")]
    p = [pg_auto(auto_row(1)), pg_auto(auto_row(2, datatype=2, down_code="1004")), pg_auto(auto_row(4))]
    r = _run_result({"autodata": run_auto(s, p)})
    md = render(r)
    for heading in ("## 1. Run info", "## 2. Verdict per machine", "## 3. Data coverage",
                    "autodata: record counts", "autodata: matching", "Columns with differences",
                    "Known (by-design) differences", "Findings"):
        assert heading in md
    assert "**Overall: FAIL.**" in md

    path = write_workbook(r, tmp_path / "out.xlsx")
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames[0] == "Summary"
    assert {"autodata_compare", "autodata_mismatches", "autodata_invalid", "coverage", "machines",
            "column_map"} <= set(wb.sheetnames)
    ws = wb["autodata_compare"]
    header = [c.value for c in ws[1]]
    assert header[:4] == ["machine", "st_time", "datatype", "occurrence"]
    assert {"down_code_sql", "down_code_pg", "down_code_ok"} <= set(header)
    assert len(ws.conditional_formatting) > 0
    # ordered by machine, then time
    times = [ws.cell(row=i, column=2).value for i in range(2, ws.max_row + 1)]
    assert times == sorted(times)
    assert ws.max_row - 1 == 4       # 2 paired + 1 only SQL Server + 1 only Postgres
    # column widths are in Excel characters, not pixels-read-as-characters (was ~2 chars wide)
    assert ws.column_dimensions["A"].width >= 17                       # machine
    assert ws.column_dimensions["B"].width >= 23                       # st_time
    inv = wb["autodata_invalid"]
    assert all(inv.column_dimensions[c].width >= 8 for c in "ABCDEFGH")


def test_one_report_per_row_names_and_header():
    from oee_validator.run import stem_for
    day = Window(datetime(2026, 10, 10, 6), datetime(2026, 10, 11, 6))
    used: set[str] = set()
    assert stem_for(None, "Machine-01", day, used) == "oee_validation_Machine-01_2026-10-10_0600_to_2026-10-11_0600"
    assert stem_for(None, "Machine/01", day, used) == "oee_validation_Machine_01_2026-10-10_0600_to_2026-10-11_0600"
    assert stem_for("plant1", "Machine-01", day, used).startswith("plant1_Machine-01_")
    assert stem_for(None, "Machine-01", day, used).endswith("~2")          # never overwrites within a run

    one = machines((("Machine-01", "1001", "1001", "1"),))
    r = RunResult(window=day, machines=one, strict=True, run_at=datetime(2026, 10, 12, 9, 0),
                  servers={}, tables={"autodata": run_auto([auto_row(1)], [pg_auto(auto_row(1))], m=one)}, job=3)
    r.findings = findings(r)
    md = render(r)
    assert md.startswith("# OEE migration validation: Machine-01, 2026-10-10 06:00 to 2026-10-11 06:00")
    assert "| machines.csv row | 3 |" in md
    assert "&& a.cycletime_range" in md                                       # the selection rule is shown


def test_sheet_truncation_is_noted(tmp_path, monkeypatch):
    from oee_validator.core import excel
    monkeypatch.setattr(excel, "EXCEL_MAX_ROWS", 100)
    rows = [auto_row(i) for i in range(150)]
    r = _run_result({"autodata": run_auto(rows, [pg_auto(x) for x in rows])})
    wb = openpyxl.load_workbook(write_workbook(r, tmp_path / "out.xlsx"))
    assert wb["autodata_compare"].max_row == 101                 # header + 100 kept rows
    cells = [str(c.value) for row in wb["Summary"].iter_rows() for c in row if c.value is not None]
    assert any("'autodata_compare' truncated to the first 100 of 150 rows" in c for c in cells)


# --------------------------------------------------------------------------- review fixes

def _raw_row(i, status=1, dt=1, machine="1001"):
    st = T0.replace(minute=i)
    return {SOURCE_ID: i, "machine": machine, "component": "C1", "operation": "10", "operator": "7",
            "datatype": dt, "st_time": st, "nd_time": st.replace(second=30), "down_code": None,
            "part_count": 1 if dt == 1 else None, "work_order_number": None, "serial_number": None,
            "heat_code": None, "supervisor_code": None, "status": status}


def test_lenient_master_id_with_leading_zeros_is_not_relinked():
    """Lenient mode strips leading zeros from the record id; the master id must match that."""
    s = [auto_row(1, component="0007")]
    ok = [pg_auto(auto_row(1, component="0007"), component_iot_id="5", master_component_interface_id="0007")]
    assert run_auto(s, ok, strict=False).metrics.invalid.height == 0
    assert run_auto(s, ok, strict=True).metrics.invalid.height == 0
    wrong = [pg_auto(auto_row(1, component="0007"), component_iot_id="5", master_component_interface_id="0008")]
    reasons = run_auto(s, wrong, strict=False).metrics.invalid["reason"].to_list()
    assert reasons == ["component iot id 5 is linked to master '8' but the record carries '7'"]


def test_findings_name_the_right_table_in_a_multi_table_run():
    raw = [_raw_row(1), _raw_row(2)]
    raw_tr = compare_table(RAW, to_raw(raw, RAW), to_raw(raw, RAW), MACHINES, ALIASES)
    auto_tr = run_auto([auto_row(1)], [pg_auto(auto_row(1), component_iot_id="77",
                                               master_component_interface_id=None)])
    f = findings(_run_result({"rawdata": raw_tr, "autodata": auto_tr}))
    inv = [x for x in f if "invalid records differ" in x]
    assert inv == ["Machine-01 / autodata: invalid records differ - 0 in SQL Server, 1 in Postgres "
                   "(see the autodata_invalid sheet)."]


def test_unlisted_machines_get_distinct_order_by_name():
    m = machines((("Machine-01", "1001", "1001", "1"),))
    rows = [auto_row(1, machine="1003"), auto_row(2, machine="1002"), auto_row(3)]
    tr = run_auto(rows, [pg_auto(x) for x in rows], m=m)
    assert tr.result.machine_summary["machine"].to_list() == [
        "Machine-01", "<unlisted machine 1002>", "<unlisted machine 1003>"]


def test_datatypes_argument():
    import argparse
    from oee_validator.run import _datatypes
    tables = ["rawdata", "autodata"]
    assert _datatypes(["rawdata=all"], tables) == {"rawdata": []}
    assert _datatypes(["rawdata=1, 2,42"], tables) == {"rawdata": [1, 2, 42]}
    for bad in (["rawdata="], ["rawdata= , "], ["rawdata=1,x"], ["nosuch=1"], ["rawdata"]):
        with pytest.raises(argparse.ArgumentTypeError):
            _datatypes(bad, tables)


@pytest.mark.parametrize("v, expected", [
    ('"secret"', "secret"), ("'secret'", "secret"), ('abc"', 'abc"'), ("\"he said 'hi'\"", "he said 'hi'"),
    ("'x\"", "'x\""), ('"', '"'), ("plain", "plain"), ('""', "")])
def test_dotenv_unquote(v, expected):
    from oee_validator.core.config import _unquote
    assert _unquote(v) == expected
