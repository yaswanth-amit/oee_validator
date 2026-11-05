"""The shareable Markdown summary: the same sections, in the same order, every run."""
from __future__ import annotations

import polars as pl

from .compare import DIFFERENT, IDENTICAL, ONLY_PG, ONLY_SQL
from .pipeline import RunResult, window_rule


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v).replace("|", "\\|").replace("\n", " ")


def md_table(headers: list[str], rows: list[list], right: set[int] | None = None) -> str:
    right = right or set()
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---:" if i in right else "---" for i in range(len(headers))) + "|"]
    out += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return "\n".join(out)


def _pair(a, b) -> str:
    return f"{a} / {b}"


def render(r: RunResult) -> str:
    w = r.window
    parts: list[str] = []
    parts.append(f"# OEE migration validation: {r.machine_label}, {w.start:%Y-%m-%d %H:%M} to {w.end:%Y-%m-%d %H:%M}")
    overall = "PASS" if r.passed else "FAIL"
    parts.append(f"**Overall: {overall}.** "
                 + ("Every machine matches in every compared table (known gaps excluded)."
                    if r.passed else "At least one machine has unexplained differences - see Findings."))

    # 1. run info
    rows = [["Machine", r.machine_label],
            ["machines.csv row", f"{r.job}"],
            ["Logical day", f"{w.start:%Y-%m-%d %H:%M:%S} (inclusive) to {w.end:%Y-%m-%d %H:%M:%S} (exclusive)"],
            ["SQL Server", r.servers.get("sqlserver", "")],
            ["Postgres", r.servers.get("postgres", "")],
            ["Compare mode", "strict (values exactly as stored)" if r.strict
             else "lenient (trimmed, blank = NULL, leading zeros in ids ignored)"],
            ["Run at", f"{r.run_at:%Y-%m-%d %H:%M:%S}  ({r.seconds:,.1f} s)"]]
    for name, t in r.tables.items():
        spec = t.result.spec
        rows.append([f"{name} datatypes", ", ".join(map(str, t.datatypes)) or "all"])
        rule = window_rule(spec)
        rows.append([f"{name} records selected (SQL Server)", f"`{rule['sqlserver']}`"])
        rows.append([f"{name} records selected (Postgres)", f"`{rule['postgres']}`"])
        rows.append([f"{name} time precision",
                     "SQL Server datetime (1/300 s), applied to both sides" if spec.datetime_precision == "sqlserver_datetime"
                     else "exact (microseconds)"])
        if spec.not_compared:
            rows.append([f"{name} columns not compared", ", ".join(spec.not_compared)])
    parts.append("## 1. Run info\n\n" + md_table(["Item", "Value"], rows))

    # 2. verdict
    v = r.verdicts
    names = list(r.tables)
    vrows = []
    for row in v.iter_rows(named=True):
        unexpl = sum(int(row.get(f"{n}_unexplained") or 0) for n in names)
        vrows.append([row["machine"]] + [row.get(n) or "" for n in names] + [unexpl])
    parts.append("## 2. Verdict per machine\n\n"
                 "PASS means every record matches. PASS (known gaps only) means the only differences are "
                 "listed known gaps. FAIL means at least one unexplained difference.\n\n"
                 + md_table(["Machine"] + names + ["Unexplained differences"], vrows, right={len(names) + 1}))

    # 3. coverage
    crow = []
    for name, t in r.tables.items():
        cov, ov = t.metrics.coverage, t.metrics.overlap
        for m in ov["machine"].to_list():
            s = cov.filter((pl.col("machine") == m) & (pl.col("side") == "sqlserver")).row(0, named=True)
            p = cov.filter((pl.col("machine") == m) & (pl.col("side") == "postgres")).row(0, named=True)
            o = ov.filter(pl.col("machine") == m).row(0, named=True)
            crow.append([m, name, s["records"], _span(s), p["records"], _span(p),
                         f"{o['overlap_from']} to {o['overlap_to']}" if o["has_overlap"] else "**none**"])
    parts.append("## 3. Data coverage\n\nFirst and last record time inside the window, per system.\n\n"
                 + md_table(["Machine", "Table", "SQL Server records", "SQL Server period", "Postgres records",
                             "Postgres period", "Overlap"], crow, right={2, 4}))

    sec = 4
    for name, t in r.tables.items():
        res, met = t.result, t.metrics
        # counts
        metrics = met.counts["metric"].unique(maintain_order=True).to_list()
        rows = []
        for m in met.counts["machine"].unique(maintain_order=True).to_list():
            sub = {x["metric"]: x for x in met.counts.filter(pl.col("machine") == m).iter_rows(named=True)}
            rows.append([m] + [_pair(sub[k]["sqlserver"], sub[k]["postgres"]) if k in sub else "" for k in metrics])
        parts.append(f"## {sec}. {name}: record counts (SQL Server / Postgres)\n\n" + md_table(["Machine"] + metrics, rows))
        sec += 1
        # matching
        rows = []
        for ms in res.machine_summary.iter_rows(named=True):
            rows.append([ms["machine"], ms["verdict"], ms["paired"], ms[IDENTICAL], ms[DIFFERENT],
                         f"{ms[ONLY_SQL]:,} ({ms['only_sqlserver_known']:,})",
                         f"{ms[ONLY_PG]:,} ({ms['only_postgres_known']:,})", ms["unexplained"]])
        body = md_table(["Machine", "Verdict", "Paired", "Identical", "Different", "Only SQL Server (known)",
                         "Only Postgres (known)", "Unexplained"], rows, right={2, 3, 4, 7})
        inv = met.invalid.group_by(["machine", "side", "reason"]).agg(pl.col("source_id").n_unique().alias("n"))
        if inv.height:
            piv = inv.pivot(on="side", index=["machine", "reason"], values="n").fill_null(0)
            for s in ("sqlserver", "postgres"):
                if s not in piv.columns:
                    piv = piv.with_columns(pl.lit(0).alias(s))
            irows = [[x["machine"], x["reason"], x["sqlserver"], x["postgres"]]
                     for x in piv.sort(["machine", "reason"]).iter_rows(named=True)]
            body += "\n\n**Invalid records by reason**\n\n" + md_table(
                ["Machine", "Reason", "SQL Server", "Postgres"], irows, right={2, 3})
        else:
            body += "\n\nNo invalid records on either side."
        parts.append(f"## {sec}. {name}: matching\n\n" + body)
        sec += 1

    # columns with differences
    blocks = []
    for name, t in r.tables.items():
        cs = t.result.column_summary.filter(pl.col("records_differing") > 0)
        if cs.height:
            blocks.append(f"**{name}**\n\n" + md_table(
                ["Column", "Records", "% of paired", "Example (SQL Server vs Postgres)"],
                [[x["column"], x["records_differing"], f"{x['pct_of_paired']:.3f}",
                  x["example (sqlserver vs postgres)"]] for x in cs.iter_rows(named=True)], right={1, 2}))
        else:
            blocks.append(f"**{name}**: no column differs in any paired record.")
    parts.append(f"## {sec}. Columns with differences\n\n" + "\n\n".join(blocks))
    sec += 1

    # known gaps
    rows = []
    for name, t in r.tables.items():
        for x in t.result.known_summary.iter_rows(named=True):
            rows.append([name, x["reason"], "SQL Server" if x["side"] == ONLY_SQL else "Postgres", x["records"]])
    parts.append(f"## {sec}. Known (by-design) differences\n\nThese records exist in one system only by design. "
                 "They are listed in the workbook but do not fail a machine.\n\n"
                 + (md_table(["Table", "Rule", "Present only in", "Records explained"], rows, right={3})
                    if rows else "No known-difference rules are configured."))
    sec += 1

    parts.append(f"## {sec}. Findings\n\n" + "\n".join(f"- {f}" for f in r.findings))
    parts.append("Definitions: a *production cycle* is datatype 1 and a *down record* datatype 2. An *invalid* "
                 "rawdata record has a status other than processed / not processed; an invalid autodata record "
                 "has a component, operation, operator or down code that does not resolve in that system's master "
                 "data. *Unexplained* = paired records with a differing value plus one-sided records with no known "
                 "reason. Totals are for orientation; the record comparison itself is exact.")
    return "\n\n".join(parts) + "\n"


def _span(row: dict) -> str:
    if not row["first"]:
        return "no records"
    return f"{row['first']} to {row['last']}"
