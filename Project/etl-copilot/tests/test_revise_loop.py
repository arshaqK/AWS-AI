"""Step 3.4 gate: run -> validate -> revise, driven by a deliberately incomplete request.

The request leaves out one vendor convention (1900-01-01 means "unknown date"). The
Script Writer cannot know it, so its first draft keeps those dates, the run fails rule
R4, and the failure report (with the offending values) goes back to the Script Writer.
The loop passes when a revised draft, approved again, runs clean.

Here the script plays the engineer: it types APPROVE for each draft. In step 3.5 the
Supervisor drives this loop and the approvals come from you.

Costs: 2+ Script Writer runs and 2+ Glue runs (roughly 10-20 cents, ~10 minutes).
The failing run briefly leaves bad dates in staging/; the passing run overwrites them.
Before any Glue run, draft 1 is dry-run locally: if it already handles the placeholder,
the loop cannot be exercised and the test stops without spending on Glue.

Usage (from app/EtlCopilot/):
    uv run --with "pandas<3" python ../../tests/test_revise_loop.py
"""
import re
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dry_run_draft import KEY, load_module, read_raw  # noqa: E402  (also adds app/EtlCopilot to sys.path)

import approvals  # noqa: E402
from agents.execution_agent import execution_agent  # noqa: E402
from agents.script_writer import script_writer  # noqa: E402
from aws_session import execution_session  # noqa: E402
from config import JOB_NAME  # noqa: E402
from tools.drafts import read_draft  # noqa: E402
from tools.execution import start_glue_job, validate_output  # noqa: E402

MAX_REVISIONS = 3
# Deliberately missing: "1900-01-01 is the vendor's placeholder for an unknown date".
TASK = ("Write the ETL for the Trailhead Outfitters orders export in landing/orders/. "
        "Vendor convention: slash dates are DD/MM/YYYY.")

results = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def draft_id_from(reply: str) -> str:
    match = re.search(r"DRAFT_ID:\W*([0-9a-f]{12})", reply)  # tolerates **bold** and `code` markup
    if not match:
        sys.exit(f"FAIL  no DRAFT_ID in the Script Writer's reply:\n{reply}")
    return match.group(1)


def real_old_date_survives(draft_id: str):
    """A genuine 1999-12-31 order must not be silently nulled by a too-broad date fix.

    Kept, or a loud ValueError, are both acceptable; quietly becoming null is not.
    """
    raw = read_raw()
    extra = raw.iloc[[0]].copy()
    extra["order_id"], extra["order_date"] = "ORD-999999", "1999-12-31"
    try:
        out = load_module("probe", read_draft(draft_id)).clean(pd.concat([raw, extra], ignore_index=True))
    except ValueError as e:
        return True, f"raised ValueError (loud): {e}"
    value = out.loc[out["order_id"] == "ORD-999999", "order_date"].iloc[0]
    kept = value is not None and str(value)[:10] == "1999-12-31"
    return kept, f"1999-12-31 -> {value!r}"


def latest_run():
    return execution_session().client("glue").get_job_runs(JobName=JOB_NAME, MaxResults=1)["JobRuns"][0]


def finish():
    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if results and all(results) else 1)


t0 = time.time()
print("=== draft 1 (request without the placeholder convention) ===")
reply = script_writer("orders", TASK)
print(reply)
draft = draft_id_from(reply)

# Free prediction before paying for Glue: does draft 1 keep the 1900 dates?
try:
    out = load_module("draft1", read_draft(draft)).clean(read_raw())
    kept = int((out["order_date"].astype(str) == "1900-01-01").sum())
    print(f"\nlocal dry run of draft 1: {kept} rows keep order_date 1900-01-01")
    if kept == 0:
        check("draft 1 is incomplete, so the loop can be exercised", False,
              "the Script Writer already nulled the placeholder; no Glue run spent")
        finish()
except Exception as e:  # clean() crashing locally means the job will FAIL: also a valid loop trigger
    print(f"\nlocal dry run of draft 1 raised {type(e).__name__}: {e} (the Glue job should fail too)")

history = []
for attempt in range(1, MAX_REVISIONS + 2):
    print(f"\n=== run {attempt}: draft {draft} ===")
    if attempt > 1:
        check(f"draft {draft} refused before its own approval", start_glue_job(draft).startswith("REFUSED"))
    before = latest_run()["Id"]
    approvals.handle_human_message(f"APPROVE {draft}")  # the test stands in for the engineer
    report = execution_agent(draft)
    print(report)

    run = latest_run()  # judge by what happened in AWS, not by the agent's wording
    started = run["Id"] != before
    validation = validate_output("orders") if started and run["JobRunState"] == "SUCCEEDED" else None
    failed_rules = [r for r, v in (validation or {}).get("rules", {}).items() if not v["passed"]]
    history.append({"draft": draft, "run_state": run["JobRunState"] if started else "not started",
                    "failed_rules": failed_rules, "passed": bool(validation and validation["passed"])})
    check(f"run {attempt} started and its approval was used up", started and draft not in approvals.pending())
    if history[-1]["passed"] or attempt > MAX_REVISIONS:
        break

    print(f"\n=== revision {attempt}: the failure report goes back to the Script Writer ===")
    revise = script_writer("orders",
        f"Revise draft {draft}. When run it failed. Execution report, verbatim:\n{report}\n"
        "Fix the cause shown there. Ask nothing; keep everything else unchanged.")
    print(revise)
    new_draft = draft_id_from(revise)
    check("revision is a new draft", new_draft != draft, f"{draft} -> {new_draft}")
    check("revision explains what changed", "changed since" in revise.lower())
    flagged = [line for line in revise.splitlines() if "NEEDS CONFIRMATION" in line.upper()]
    check("revision flags its guess for the engineer", any("1900-01-01" in line for line in flagged),
          flagged[0].strip()[:160] if flagged else "no NEEDS CONFIRMATION line")
    survives, detail = real_old_date_survives(new_draft)
    check("revision fixes the value, not the rule's range (real 1999 date not erased)", survives, detail)
    draft = new_draft

print("\n=== loop history ===")
for i, h in enumerate(history, 1):
    print(f"  run {i}: draft {h['draft']}  {h['run_state']:<12} "
          f"{'PASSED' if h['passed'] else 'failed: ' + (', '.join(h['failed_rules']) or 'job error')}")

first = history[0]
check("run 1 failed (job error or a rule)", not first["passed"],
      first["run_state"] + (f" / {first['failed_rules']}" if first["failed_rules"] else ""))
check("run 1 was caught by R4 (the placeholder dates)",
      "R4_valid_dates" in first["failed_rules"] or first["run_state"] != "SUCCEEDED")
check(f"a revised draft passed within {MAX_REVISIONS} revisions", history[-1]["passed"] and len(history) > 1,
      f"{len(history) - 1} revision(s)")
if history[-1]["passed"]:
    info = validate_output("orders")["info"]
    expected = KEY["expected_clean_output"]
    check("final output matches the answer key",
          (info["null_order_dates"], info["null_unit_prices"], info["invalid_emails"])
          == (expected["rows_with_invalid_order_date"], expected["rows_with_unparseable_unit_price"],
              KEY["counts"]["email_malformed"]), str(info))
print(f"[total {time.time() - t0:.0f}s]")
finish()
