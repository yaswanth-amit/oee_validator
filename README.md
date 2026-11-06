# OEE Validator: SQL Server vs Postgres

A batch tool that checks, machine by machine and record by record, that the new Postgres
pipeline holds exactly the same `rawdata` and `autodata` records as the SQL Server pipeline.
It is built on Polars. You give it a machine list where each row is one machine name and one logical
day, and it writes two files per row:

| File | For |
|---|---|
| `reports/oee_validation_<machine>_<start>_to_<end>.md` | The summary of that machine and day, to read and share |
| `reports/oee_validation_<machine>_<start>_to_<end>.xlsx` | Every record of that machine and day side by side, with differences highlighted |

It replaces the Streamlit prototype in `oee_compare/`. It keeps the prototype's field mappings and adds the batch report from `oee_compare/README_PLAN.md`, Part 2.

## Setup

```
pip install -r oee_validator/requirements.txt
copy oee_validator\.env.example oee_validator\.env     # then fill in both passwords
```

Passwords come only from `oee_validator/.env` or environment variables. If neither is set,
`oee_compare/.env` is used as a fallback. The server settings are in `config/connections.yaml`.

## Run

From the `Raw OEE` folder:

```
python -m oee_validator --machines oee_validator/machines.csv
python -m pytest oee_validator/tests -q            # engine tests, no database needed
```

| Option | Meaning |
|---|---|
| `--machines` | The machine list with one logical day per row (required, see below) |
| `--tables rawdata` | Compare only some tables (default: rawdata, then autodata) |
| `--datatypes rawdata=1,2,42` | Override a table's datatypes (`TABLE=all` turns the filter off) |
| `--lenient` | Trim spaces, treat blank as NULL, ignore leading zeros in ids |
| `--exact-time` | Compare timestamps to the microsecond (see "Time precision") |
| `--sheet-per-machine` | One comparison sheet per machine instead of one per table |
| `--out-dir` | Where the files go |
| `--name` | File name prefix instead of `oee_validation` |

At the end the console shows one line per row with its verdict per table. Exit code `0` means every
row passes (known gaps allowed). `1` means at least one unexplained difference. `2` means an error,
such as a bad machine list or a connection problem. A row with more records than `max_rows_per_side`
is skipped, shown as `ERROR`, and the run continues with the next row (exit code `2`).
So the tool can run as a scheduled job.

### machines.csv

```csv
machine_name,logical_day_start,logical_day_end
Machine-01,2026-10-10 06:00:00,2026-10-11 06:00:00
Machine-01,2026-10-11 06:00:00,2026-10-12 06:00:00
Machine-02,2026-10-10 06:00:00,2026-10-11 06:00:00
```

Each row is one job with its own report. The logical day is `[logical_day_start, logical_day_end)`:
the start is included, the end is not. Timestamps must be `YYYY-MM-DD HH:MM:SS` (seconds optional).
If you edit the file in Excel, check that Excel has not rewritten them as `10-10-2026 06:00` or
`10/10/2026`: those are rejected rather than guessed.

The ids are not in the file. Before any data is read, each `machine_name` is looked up in both machine
masters (trimmed, case-insensitive):

| System | Looked up in | Gives |
|---|---|---|
| SQL Server | `dbo.machineinformation.machineid` | `InterfaceID` |
| Postgres | `master_data.machine_information.machine_id` | `interface_id`, `iot_id` |

The run stops if a name is missing from either master, appears more than once in one, or has no id there.
The resolved ids are shown in the Excel `machines` sheet. A machine can appear on several rows (one per day);
the same machine and day twice is rejected. Records are paired on `machine_name`, so a machine pairs correctly
even if its ids differ between the systems. Postgres `autodata` is filtered on the resolved `postgres_iot_id`,
so its index is used. Older files that still have the id columns load fine; those columns are ignored.

### Which records belong to a logical day

| Table | SQL Server | Postgres |
|---|---|---|
| rawdata | `Sttime` in `[start, end)` | `st_time` in `[start, end)` |
| autodata | SQL Server's own 4 cases on `msttime` / `ndtime` (below) | `tsrange(start, end, '[)') && cycletime_range`, plus zero-length rows inside the day |

autodata follows the definition SQL Server's OEE procedures use (`s_GetCockpitData`,
`s_Get_production_down_shiftwise`). With S = logical_day_start and E = logical_day_end, a record belongs to the day when:

1. `msttime >= S AND ndtime <= E` (fully inside)
2. `msttime < S AND ndtime > S AND ndtime <= E` (crosses the start)
3. `msttime >= S AND msttime < E AND ndtime > E` (crosses the end)
4. `msttime < S AND ndtime > E` (spans the whole day)

A NULL `msttime` or `ndtime` never matches. SQL Server has no range type, so Postgres uses its
generated column `cycletime_range = tsrange(mst_time, nd_time)` (GiST-indexed). For a normal record
the 4 cases are exactly range overlap. A zero-length record (`mst_time = nd_time`) has an empty range
that overlaps nothing, so it is added explicitly when it lies inside `[S, E]`, as case 1 does. Postgres
cannot store `mst_time > nd_time`; such SQL Server rows are still selected by case 1 and show up as
`only_sqlserver`. A cycle that crosses the day boundary appears in the reports of both days. The rule is the
`window_filter` in `config/tables/autodata.yaml`, and each report shows the exact rule used.

## How the comparison works

1. **Fetch.** For each row, both databases are queried in parallel with the same machine, logical day
   and datatypes. Only SELECT statements are sent, and the Postgres session is read-only.
2. **Normalise.** Every value becomes exact text in Polars. `1.50000` equals `1.5`, but
   `1.50001` does not. Status codes are compared by meaning (`aliases.yaml`). Predefined
   down codes are matched by name (`NO_DATA` = `no_data`). Postgres autodata iot ids are joined
   back to interface ids.
3. **Pair.** Records pair on machine name + datatype + start time. When the same key repeats
   on one side, an occurrence number is added, in source-id order.
4. **Compare.** Every mapped column is compared exactly. Each record ends up as
   `identical`, `different`, `only_sqlserver` or `only_postgres`.
5. **Explain.** A one-sided record that matches a `known_differences` rule carries its
   reason. It is still listed, but it does not fail the machine.

**Verdict per machine and table:** `PASS` means every record matches.
`PASS (known gaps only)` means the only differences are listed known gaps. `FAIL` means at least one
unexplained difference. `NO DATA` means neither system has records in the logical day.

### Time precision

SQL Server `RawData.Sttime` and `autodata.sttime` are `datetime` columns, which are stored in steps of
1/300 s (.000, .003, .007). Postgres keeps microseconds. By default, Postgres timestamps are
rounded the same way before they are compared. That is exactly the value SQL Server would have stored
for the same input string, so a real difference is still caught. Use `--exact-time` to switch this off.

### Component and operation without a master

Machines can send a component or operation before its master exists. Postgres then stores
the interface id in `autodata.component_interface_id` / `operation_interface_id` and leaves the
iot id empty until it is linked later. SQL Server stores the interface id directly. So the
compared Postgres value is the stored interface id, falling back to the master's id for older
records that have none. Whether the iot id is missing or linked to the wrong master is reported
separately as an invalid record, so a missing master never shows up as a false difference.

### What is counted

| Term | rawdata | autodata |
|---|---|---|
| Production cycle / down | datatype 1 / 2 | datatype 1 / 2 |
| ICD | datatype 42 | Postgres `is_icd`. SQL Server has no flag, so it uses the `s_GetCockpitData` rule: a down (2) strictly inside a cycle (1) that took longer than its cycle time |
| Invalid | Status meaning other than processed / not processed | Component, operation, operator or down code (downs only) does not resolve. SQL Server: missing from its master (the shift aggregation drops these rows). Postgres: iot id empty (no master yet), iot id with no master row, or iot id linked to a master whose interface id differs from the one the record carries |
| Totals | – | Sum of cycle_time, load_unload and part_count for cycles; sum of (nd_time − st_time) for downs |

## Configuration

| File | Holds |
|---|---|
| `config/tables/rawdata.yaml`, `autodata.yaml` | Column mapping, pairing key, machine filter, time precision, known differences, report counts |
| `config/aliases.yaml` | Status-code meanings and predefined down-code spellings |
| `config/connections.yaml` | Servers and the row limit per side (`max_rows_per_side`) |

To add a table pair, copy `autodata.yaml`. It then appears in the run automatically.

## Code layout

| File | Does |
|---|---|
| `run.py` | Command line, exit codes |
| `core/pipeline.py` | One run: check machines, fetch, compare, metrics, findings |
| `core/db.py` | Read-only connections and query building |
| `core/normalize.py` | Exact canonical text per column type (vectorised Polars, exact fallback) |
| `core/compare.py` | Pairing, comparison, known gaps, verdicts |
| `core/metrics.py` | Counts, totals, coverage, invalid records |
| `core/markdown.py`, `core/excel.py` | The two output files |

## Limits and open points

- **Excel size.** Writing .xlsx takes about 5 µs per cell, so a window with a million records takes a few minutes
  to write. A sheet over Excel's row limit is cut, and a note is added to the Summary.
- **SQL Server login.** The connection uses `sa`. `ApplicationIntent=ReadOnly` does not block
  writes on a standalone server. The tool only sends SELECTs, but a dedicated read-only login is the
  real guarantee.
- **Operation check.** The SQL Server invalid-operation check tests the operation id against
  `componentoperationpricing` alone, not the machine + component + operation combination.
- **Not compared yet.** These autodata columns are commented out in the YAML and named in every report: post, supervisor_code, remarks,
  action_taken, is_modified, kwh, net_kwh.
- **autodata.cycletime_range.** In Postgres this is `GENERATED ALWAYS AS tsrange(mst_time, nd_time)`
  (bounds `[)`) with a GiST index. The Postgres filter relies on that definition to reproduce
  SQL Server's 4 cases; if it changes, change `window_filter` in `autodata.yaml` so both sides keep
  selecting the same records.
  SQL Server has no index on `msttime`/`ndtime` (only `mc, sttime`), so its autodata query scans
  each machine's rows; fine for one machine and day, slower for long windows.
- **Setup Change.** In `aliases.yaml`, the `Setup Change` key is a placeholder for this
  installation's real interface id.
- **Production use.** Results from the test databases (`TPM_Baseline`, `pragathi_qa`) need to be repeated
  against the production databases before they are relied on. Changes to the mappings should go
  through normal review.
