"""Run log: one JSON record per Glue run, and the first-run success rate built from them.

Records are written by code (the Execution Agent's wrapper), from what Glue and Athena
report - never from a model's wording - to s3://etl-copilot-ak/reports/runs/<dataset>/<run_id>.json.

Onboarding cycles: the runs of one dataset under one spec, up to and including the first
run that passes validation. The next run after a pass (or under a changed spec) starts a
new cycle. A cycle is a FIRST-RUN SUCCESS when its attempt 1 passed.

Engineer CLI (from app/EtlCopilot/):   uv run python -m tools.runlog
"""
import json
import sys
from datetime import datetime, timezone
from typing import List, Optional

from aws_session import execution_session
from config import BUCKET, JOB_NAME, REPORTS_PREFIX
from tools.specs import load_spec, spec_id_for

RUNS_PREFIX = f"{REPORTS_PREFIX}runs/"


def _s3():
    return execution_session().client("s3")


def list_runs(dataset: Optional[str] = None) -> List[dict]:
    """Every run record (optionally for one dataset), oldest first."""
    prefix = f"{RUNS_PREFIX}{dataset}/" if dataset else RUNS_PREFIX
    s3, records = _s3(), []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json"):
                records.append(json.loads(s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()))
    return sorted(records, key=lambda r: r["started_at"])


def next_attempt(previous: List[dict], spec_id: str) -> int:
    """Attempt number of a new run: runs since the last pass under the same spec, plus one."""
    attempt = 0
    for r in previous:
        if r["spec_id"] != spec_id:
            attempt = 0  # a different contract is a different onboarding
            continue
        attempt = 0 if r.get("outcome") == "passed" else attempt + 1
    return attempt + 1


def outcome_of(job_state: str, validation: Optional[dict]) -> str:
    if job_state != "SUCCEEDED":
        return "job_failed"
    if validation is None:
        return "not_validated"
    return "passed" if validation.get("passed") else "failed_validation"


def record_run(dataset: str, draft_id: str, run_id: str, validation: Optional[dict]) -> dict:
    """Write the record of one finished (or timed-out) Glue run. Returns it."""
    run = execution_session().client("glue").get_job_run(JobName=JOB_NAME, RunId=run_id)["JobRun"]
    spec_id = spec_id_for(load_spec(dataset))
    record = {
        "dataset": dataset,
        "spec_id": spec_id,
        "draft_id": draft_id,
        "run_id": run_id,
        "attempt": next_attempt(list_runs(dataset), spec_id),
        "started_at": run["StartedOn"].astimezone(timezone.utc).isoformat(),
        "finished_at": run["CompletedOn"].astimezone(timezone.utc).isoformat() if run.get("CompletedOn") else None,
        "job_state": run["JobRunState"],
        "job_error": run.get("ErrorMessage"),
        "execution_time_s": run.get("ExecutionTime"),
        "validation": None if validation is None else {
            "passed": validation["passed"],
            "failed_rules": [n for n, r in validation["rules"].items() if not r["passed"]],
            "info": validation.get("info", {}),
        },
    }
    record["outcome"] = outcome_of(record["job_state"], validation)
    _s3().put_object(Bucket=BUCKET, Key=f"{RUNS_PREFIX}{dataset}/{run_id}.json",
                     Body=json.dumps(record, indent=2).encode("utf-8"), ContentType="application/json")
    return record


def cycles(records: List[dict]) -> List[List[dict]]:
    """Group one dataset's records (oldest first) into onboarding cycles."""
    out: List[List[dict]] = []
    for r in records:
        if not out or out[-1][-1]["outcome"] == "passed" or out[-1][-1]["spec_id"] != r["spec_id"]:
            out.append([])
        out[-1].append(r)
    return out


def summarize(records: Optional[List[dict]] = None) -> dict:
    records = list_runs() if records is None else records
    per_dataset = {}
    for dataset in sorted({r["dataset"] for r in records}):
        cs = cycles([r for r in records if r["dataset"] == dataset])
        finished = [c for c in cs if c[-1]["outcome"] == "passed"]
        per_dataset[dataset] = {
            "runs": sum(len(c) for c in cs),
            "cycles": len(cs),
            "cycles_passed": len(finished),
            "first_run_successes": sum(c[0]["outcome"] == "passed" for c in cs),
            "runs_to_pass": [len(c) for c in finished],
            "failures": [{"attempt": r["attempt"], "draft_id": r["draft_id"], "outcome": r["outcome"],
                          "failed_rules": (r["validation"] or {}).get("failed_rules", []),
                          "job_error": (r["job_error"] or "")[:200] or None}
                         for c in cs for r in c if r["outcome"] != "passed"],
        }
    total = sum(d["cycles"] for d in per_dataset.values())
    firsts = sum(d["first_run_successes"] for d in per_dataset.values())
    return {"first_run_success_rate": (firsts / total) if total else None,
            "first_run_successes": firsts, "cycles": total, "datasets": per_dataset,
            "generated_at": datetime.now(timezone.utc).isoformat()}


def print_report(summary: dict) -> None:
    rate = summary["first_run_success_rate"]
    print(f"First-run success rate: {'n/a' if rate is None else f'{rate:.0%}'} "
          f"({summary['first_run_successes']} of {summary['cycles']} onboarding cycles)\n")
    for name, d in summary["datasets"].items():
        print(f"{name}: {d['runs']} runs, {d['cycles']} cycles, {d['cycles_passed']} passed, "
              f"first-run successes {d['first_run_successes']}/{d['cycles']}, runs to pass {d['runs_to_pass']}")
        for f in d["failures"]:
            why = ", ".join(f["failed_rules"]) or f["job_error"] or f["outcome"]
            print(f"   attempt {f['attempt']} draft {f['draft_id']}: {f['outcome']} - {why}")


if __name__ == "__main__":
    summary = summarize()
    print_report(summary)
    if "--save" in sys.argv:
        _s3().put_object(Bucket=BUCKET, Key=f"{REPORTS_PREFIX}run_summary.json",
                         Body=json.dumps(summary, indent=2).encode("utf-8"), ContentType="application/json")
        print(f"\nsaved s3://{BUCKET}/{REPORTS_PREFIX}run_summary.json")
