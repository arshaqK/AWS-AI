"""Step 4.2.5 gate: the run log and the first-run success rate.

  A. (offline) Attempt numbers and onboarding cycles are counted correctly on made-up
     records: a pass ends a cycle, a changed spec starts a new one, job failures and
     validation failures both count as failed attempts.
  B. (live, reads S3) The records written by real runs: the latest cycle for a dataset
     has consecutive attempts, its outcomes agree with Glue's own run history, and the
     report prints. Run test_revise_loop.py first to create a fail -> pass cycle.

Usage (from app/EtlCopilot/):   uv run python ../../tests/test_runlog.py [dataset]
"""
import os
import sys
from pathlib import Path

APP_DIR = Path(os.getenv("ETL_COPILOT_APP", Path(__file__).resolve().parents[1] / "app" / "EtlCopilot"))
sys.path.insert(0, str(APP_DIR))

from aws_session import execution_session  # noqa: E402
from config import JOB_NAME  # noqa: E402
from tools.runlog import cycles, list_runs, next_attempt, outcome_of, print_report, summarize  # noqa: E402

DATASET = sys.argv[1] if len(sys.argv) > 1 else "orders"
results = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def rec(ds, spec, outcome, t, draft="d"):
    return {"dataset": ds, "spec_id": spec, "outcome": outcome, "started_at": f"2026-10-02T00:{t:02d}:00+00:00",
            "attempt": None, "draft_id": draft, "validation": None, "job_error": None}


print("--- A. counting rules (offline) ---")
check("outcome: job failure", outcome_of("FAILED", None) == "job_failed")
check("outcome: validation failure", outcome_of("SUCCEEDED", {"passed": False}) == "failed_validation")
check("outcome: pass", outcome_of("SUCCEEDED", {"passed": True}) == "passed")
check("outcome: succeeded but never validated", outcome_of("SUCCEEDED", None) == "not_validated")

check("first ever run is attempt 1", next_attempt([], "s1") == 1)
history = [rec("orders", "s1", "failed_validation", 1), rec("orders", "s1", "job_failed", 2)]
check("after two failures, the next is attempt 3", next_attempt(history, "s1") == 3)
history.append(rec("orders", "s1", "passed", 3))
check("after a pass, the next run starts a new cycle (attempt 1)", next_attempt(history, "s1") == 1)
check("a changed spec starts a new cycle", next_attempt(history[:2], "s2") == 1)

records = [
    rec("orders", "s1", "failed_validation", 1), rec("orders", "s1", "passed", 2),   # cycle 1: 2 runs
    rec("orders", "s1", "passed", 3),                                                # cycle 2: first-run pass
    rec("orders", "s1", "job_failed", 4),                                            # cycle 3: still open...
    rec("orders", "s2", "passed", 5),                                                # ...spec changed: cycle 4
    rec("inventory_snapshot", "s9", "passed", 6),                                    # other dataset: 1 cycle
]
grouped = cycles([r for r in records if r["dataset"] == "orders"])
check("orders splits into 4 cycles", [len(c) for c in grouped] == [2, 1, 1, 1], str([len(c) for c in grouped]))
s = summarize(records)
check("orders: 2 first-run successes of 4 cycles",
      (s["datasets"]["orders"]["first_run_successes"], s["datasets"]["orders"]["cycles"]) == (2, 4))
check("orders: runs to pass per finished cycle", s["datasets"]["orders"]["runs_to_pass"] == [2, 1, 1])
check("overall rate = 3 of 5 cycles", (s["first_run_successes"], s["cycles"]) == (3, 5)
      and abs(s["first_run_success_rate"] - 0.6) < 1e-9, f"{s['first_run_success_rate']:.0%}")
check("failures list both failed attempts with their reason",
      [f["outcome"] for f in s["datasets"]["orders"]["failures"]] == ["failed_validation", "job_failed"])

print(f"--- B. real records for {DATASET} (S3) ---")
mine = list_runs(DATASET)
if not mine:
    check(f"there are run records for {DATASET}", False, "run test_revise_loop.py (or a real onboarding) first")
else:
    last = cycles(mine)[-1]
    check("latest cycle has consecutive attempts 1..n", [r["attempt"] for r in last] == list(range(1, len(last) + 1)),
          str([(r["attempt"], r["outcome"]) for r in last]))
    glue = execution_session().client("glue")
    agree = all(glue.get_job_run(JobName=JOB_NAME, RunId=r["run_id"])["JobRun"]["JobRunState"] == r["job_state"]
                for r in last)
    check("every record's job state matches Glue's run history", agree)
    check("validation recorded for every succeeded run",
          all(r["validation"] is not None for r in last if r["job_state"] == "SUCCEEDED"))
    print()
    print_report(summarize())

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
