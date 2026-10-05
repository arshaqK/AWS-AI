"""Data dictionary and quality report for a cleaned dataset (step 4.3).

Every number in the documents is computed here, from Glue, Athena and the run log - never
written by a model. The Documenter agent only supplies prose (what a column means and how
it was derived), which is slotted into these templates. Before anything is saved, the
rendered text is scanned for personal data and refused if it contains any.

Output: s3://etl-copilot-ak/reports/<dataset>/data_dictionary.md and quality_report.md
"""
import re
from datetime import datetime, timezone
from typing import Dict, List

from athena import run_query
from aws_session import execution_session
from config import BUCKET, CLEAN_DB, RAW_DB, REPORTS_PREFIX
from tools.execution import _key_sql, validate_output
from tools.pii import find_personal_data
from tools.profile import NULL_TOKENS
from tools.runlog import cycles, list_runs
from tools.specs import ident, literal, load_spec, paths

ORDERABLE = re.compile(r"^(int|bigint|double|decimal\(.*\)|date|timestamp)$")
CATEGORY_MAX = 20  # list the values of columns with at most this many distinct values


class DocsError(RuntimeError):
    """The dataset cannot be documented yet (no passing run) or the output was unsafe."""


def gather_facts(dataset: str) -> dict:
    """Everything numeric the two documents state, straight from AWS."""
    spec = load_spec(dataset)
    where = paths(dataset)
    runs = list_runs(dataset)
    if not runs or runs[-1]["outcome"] != "passed":
        raise DocsError(f"{dataset}: the table's latest run did not pass validation "
                        f"({runs[-1]['outcome'] if runs else 'no runs recorded'}); nothing to document yet")
    run = runs[-1]

    glue_table = execution_session().client("glue").get_table(DatabaseName=CLEAN_DB, Name=where["clean_table"])["Table"]
    columns = [{"name": c["Name"], "type": c["Type"], "partition": False}
               for c in glue_table["StorageDescriptor"]["Columns"]]
    columns += [{"name": c["Name"], "type": c["Type"], "partition": True} for c in glue_table.get("PartitionKeys", [])]
    pii_outputs = {p["output"] for p in spec.get("pii", []) if p.get("output")}

    select = ["count(*) AS total"]
    for i, c in enumerate(columns):
        col = ident(c["name"])
        select += [f"count_if({col} IS NULL) AS n{i}", f"count(DISTINCT {col}) AS d{i}"]
        if ORDERABLE.match(c["type"]):
            select += [f"CAST(min({col}) AS varchar) AS lo{i}", f"CAST(max({col}) AS varchar) AS hi{i}"]
    stats = run_query(f"SELECT {', '.join(select)} FROM {where['clean_table']}", CLEAN_DB)[0]
    total = int(stats["total"])

    key = spec["primary_key"]
    for i, c in enumerate(columns):
        c.update(nulls=int(stats[f"n{i}"]), distinct=int(stats[f"d{i}"]),
                 null_pct=round(100 * int(stats[f"n{i}"]) / total, 1) if total else 0.0,
                 low=stats.get(f"lo{i}"), high=stats.get(f"hi{i}"),
                 pii=c["name"] in pii_outputs, key=c["name"] in key)

    # values: categories only, never personal data, never free text
    shown = [c for c in columns if not c["pii"] and not ORDERABLE.match(c["type"]) and 0 < c["distinct"] <= CATEGORY_MAX]
    if shown:
        sql = " UNION ALL ".join(
            f"SELECT {literal(c['name'])} AS col, CAST({ident(c['name'])} AS varchar) AS v, count(*) AS n "
            f"FROM {where['clean_table']} WHERE {ident(c['name'])} IS NOT NULL GROUP BY 2" for c in shown)
        values: Dict[str, list] = {}
        for r in sorted(run_query(sql, CLEAN_DB), key=lambda r: -int(r["n"])):
            values.setdefault(r["col"], []).append((r["v"], int(r["n"])))
        for c in shown:
            c["values"] = values.get(c["name"], [])

    raw_key = spec.get("raw_key", key)
    tokens = ", ".join(literal(t) for t in NULL_TOKENS)
    raw_not_null = " AND ".join(f"trim(lower(CAST({ident(k)} AS varchar))) NOT IN ({tokens})" for k in raw_key)
    raw = run_query(f"SELECT count(*) AS raw_rows, count_if({raw_not_null}) AS keyed_rows, "
                    f"count(DISTINCT CASE WHEN {raw_not_null} THEN {_key_sql(raw_key, raw=True)} END) AS raw_keys "
                    f"FROM {where['raw_table']}", RAW_DB)[0]

    return {
        "dataset": dataset, "spec": spec, "paths": where, "run": run,
        "cycle": next(c for c in reversed(cycles(runs)) if c[-1]["run_id"] == run["run_id"]),
        "validation": validate_output(dataset),  # fresh: the table as it is now
        "columns": columns, "rows": total,
        "raw_rows": int(raw["raw_rows"]), "raw_keys": int(raw["raw_keys"]),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_dictionary(f: dict, prose: dict) -> str:
    spec, where, run = f["spec"], f["paths"], f["run"]
    part = spec.get("partition")
    lines = [
        f"# Data dictionary: `{CLEAN_DB}.{f['dataset']}`",
        "",
        f"> Generated by ETL Copilot on {f['generated_at']} from run `{run['run_id']}` "
        f"(draft `{run['draft_id']}`, spec `{run['spec_id']}`). Figures are computed from the table; "
        "descriptions are written by the Documenter agent from the approved spec and the script that ran.",
        "",
        prose.get("overview", spec.get("description", "")),
        "",
        "| | |", "|---|---|",
        f"| Table | `{CLEAN_DB}.{where['clean_table']}` |",
        f"| Location | `{where['staging_uri']}` (Parquet) |",
        f"| Source | `{where['landing_uri']}` (raw CSV, table `{RAW_DB}.{where['raw_table']}`) |",
        f"| Rows | {f['rows']} |",
        f"| Primary key | {', '.join(f'`{k}`' for k in spec['primary_key'])} |",
        f"| Partition | {f'`{part['name']}` - {part['rule']}' if part else 'none'} |",
        "",
        "## Columns",
        "",
        "| Column | Type | Nulls | Null % | Distinct | Range / values |",
        "|---|---|---|---|---|---|",
    ]
    for c in f["columns"]:
        if c["pii"]:
            detail = "personal data (masked) - values not shown"
        elif c.get("values"):
            detail = ", ".join(f"`{_cell(v)}` ({n})" for v, n in c["values"][:CATEGORY_MAX])
        elif c["low"] is not None:
            detail = f"{c['low']} .. {c['high']}"
        else:
            detail = ""
        name = f"`{c['name']}`" + (" (key)" if c["key"] else "") + (" (partition)" if c["partition"] else "")
        lines.append(f"| {name} | {c['type']} | {c['nulls']} | {c['null_pct']}% | {c['distinct']} | {detail} |")

    descriptions = {c["name"]: c.get("description", "") for c in spec["columns"]}
    derivations = prose.get("columns", {})
    pii_sources = {p["output"]: p for p in spec.get("pii", []) if p.get("output")}
    lines += ["", "## How each column is derived", ""]
    for c in f["columns"]:
        lines.append(f"### `{c['name']}`")
        meaning = descriptions.get(c["name"]) or (part["rule"] if c["partition"] and part else "")
        if meaning:
            lines.append(f"**Meaning:** {meaning}")
        lines.append(f"**Derived:** {derivations.get(c['name'], 'not described')}")
        if c["name"] in pii_sources:
            p = pii_sources[c["name"]]
            lines.append(f"**Personal data:** {p['treatment']} of raw `{p['column']}`" + (f" ({p['note']})" if p.get("note") else ""))
        lines.append("")
    dropped = [p for p in spec.get("pii", []) if p["treatment"] == "drop"]
    if dropped:
        lines += ["## Dropped raw columns", ""] + [f"- `{p['column']}`: {p.get('note') or 'personal data'}" for p in dropped] + [""]
    if prose.get("caveats"):
        lines += ["## Caveats", ""] + [f"- {c}" for c in prose["caveats"]] + [""]
    return "\n".join(lines)


def render_quality(f: dict) -> str:
    v, run, where = f["validation"], f["run"], f["paths"]
    rules = v["rules"]
    passed = sum(r["passed"] for r in rules.values())
    lines = [
        f"# Quality report: `{f['dataset']}`",
        "",
        f"> Generated by ETL Copilot on {f['generated_at']}. Every figure is computed from Glue, Athena "
        "and the run log; the rules were re-checked against the table when this report was generated.",
        "",
        f"## Result: {'PASSED' if v['passed'] else 'FAILED'} ({passed}/{len(rules)} rules)",
        "",
        "| | |", "|---|---|",
        f"| Raw rows (CSV, including re-sent duplicates) | {f['raw_rows']} |",
        f"| Distinct raw keys | {f['raw_keys']} |",
        f"| Clean rows | {f['rows']} |",
        f"| Raw rows not carried over (duplicates) | {f['raw_rows'] - f['rows']} |",
        f"| Run | `{run['run_id']}` - {run['job_state']}, {run['execution_time_s']} s, finished {run['finished_at']} |",
        f"| Draft / spec | `{run['draft_id']}` / `{run['spec_id']}` |",
        f"| Approval | Draft `{run['draft_id']}` ran only after the engineer's `APPROVE {run['draft_id']}`; "
        "the spec only after `APPROVE SPEC`. (Approver identity is recorded from Phase 5.) |",
        f"| Output | `{CLEAN_DB}.{where['clean_table']}` at `{where['staging_uri']}` |",
        "",
        "## Rules",
        "",
        "| Rule | Result | Detail |",
        "|---|---|---|",
    ]
    for name, r in rules.items():
        lines.append(f"| {name} | {'passed' if r['passed'] else 'FAILED'} | {_cell(r['detail'])} |")
    if v.get("info"):
        lines += ["", "## Counts reported (expected, not failures)", "", "| Count | Value |", "|---|---|"]
        lines += [f"| {k} | {n} |" for k, n in v["info"].items()]
    lines += ["", "## Onboarding history", "",
              f"This table came from attempt {run['attempt']} of its onboarding cycle.", "",
              "| Attempt | Draft | Outcome | Why it failed |", "|---|---|---|---|"]
    for r in f["cycle"]:
        why = ", ".join((r["validation"] or {}).get("failed_rules", [])) or (r["job_error"] or "")[:120]
        lines.append(f"| {r['attempt']} | `{r['draft_id']}` | {r['outcome']} | {_cell(why)} |")
    return "\n".join(lines) + "\n"


def save_docs(dataset: str, dictionary: str, quality: str) -> dict:
    """Refuse to save anything that looks like personal data; otherwise write both files."""
    hits = find_personal_data(dictionary + "\n" + quality)
    if hits:
        raise DocsError(f"refused to save docs for {dataset}: they contain {len(hits)} value(s) that "
                        f"look like personal data (first: {hits[0][:4]}...)")
    s3 = execution_session().client("s3")
    keys = {"data_dictionary": f"{REPORTS_PREFIX}{dataset}/data_dictionary.md",
            "quality_report": f"{REPORTS_PREFIX}{dataset}/quality_report.md"}
    for name, body in (("data_dictionary", dictionary), ("quality_report", quality)):
        s3.put_object(Bucket=BUCKET, Key=keys[name], Body=body.encode("utf-8"),
                      ContentType="text/markdown; charset=utf-8")
    return {k: f"s3://{BUCKET}/{key}" for k, key in keys.items()}
