"""Step 3.1 test gate: call every tool as a plain Python function (no LLM) against real AWS.

Run from app/EtlCopilot/:   uv run python ../../tests/test_tools_live.py
Costs a few Athena queries (fractions of a cent). Starts no Glue job.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(os.getenv("ETL_COPILOT_APP", ROOT / "app" / "EtlCopilot"))
KEY_PATH = Path(os.getenv("ETL_COPILOT_KEY", ROOT / "data" / "orders_export.expected.json"))
sys.path.insert(0, str(APP_DIR))
KEY = json.loads(KEY_PATH.read_text(encoding="utf-8"))  # answer key from data/generate_orders.py

from aws_session import execution_session  # noqa: E402
from config import JOB_NAME  # noqa: E402
from tools.execution import make_validation_tool, start_glue_job, wait_for_job  # noqa: E402
from tools.profile import get_dataset_profile  # noqa: E402

results = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


# 1) every call runs as the Execution role, not as you
arn = execution_session().client("sts").get_caller_identity()["Arn"]
check("tools run as EtlCopilotExecutionRole",
      ":assumed-role/EtlCopilotExecutionRole/etl-copilot-tools" in arn, arn)

# 2) profile of the raw table
profile = json.loads(get_dataset_profile("orders"))
cols = {c["name"]: c for c in profile["columns"]}
date_shapes = {s["shape"] for s in cols["order_date"]["top_shapes"]}
check("profile: row count", profile["row_count"] == KEY["rows_in_file"], f"{profile['row_count']} rows")
check("profile: 3 date layouts detected",
      {"9999-99-99", "99/99/9999", "9999-99-99a99:99:99a"} <= date_shapes, str(sorted(date_shapes)))
check("profile: every ship_country spelling found",
      set(cols["ship_country"]["values"] or {}) == set(KEY["country_raw_to_iso"]),
      f"{len(cols['ship_country']['values'] or {})} spellings")
check("profile: every order_status spelling found",
      set(cols["order_status"]["values"] or {}) == set(KEY["status_raw_to_canonical"]),
      f"{len(cols['order_status']['values'] or {})} spellings")
check("profile: unit_price null spellings found",
      {"<empty>", "N/A", "null", "-"} <= set(cols["unit_price"]["null_tokens"]),
      str(cols["unit_price"]["null_tokens"]))

# 3) validation of the current (golden) clean output
v = json.loads(make_validation_tool("orders")())
for name, r in v["rules"].items():
    check(f"validation {name}", r["passed"], str(r["detail"]))
expected = KEY["expected_clean_output"]
check("validation info matches answer key",
      (v["info"]["null_order_dates"], v["info"]["null_unit_prices"], v["info"]["invalid_emails"],
       v["info"]["distinct_countries"], v["info"]["distinct_statuses"])
      == (expected["rows_with_invalid_order_date"], expected["rows_with_unparseable_unit_price"],
          KEY["counts"]["email_malformed"], expected["distinct_ship_country_after_normalizing"],
          expected["distinct_order_status_after_normalizing"]),
      str(v["info"]))

# 4) no job can start in step 3.1
check("start_glue_job refuses", start_glue_job("000000000000").startswith("REFUSED"))

# 5) wait_for_job reads the latest real run (already finished, so it returns at once)
latest = execution_session().client("glue").get_job_runs(JobName=JOB_NAME, MaxResults=1)["JobRuns"][0]
w = json.loads(wait_for_job(latest["Id"]))
check("wait_for_job reports latest run", w["state"] == latest["JobRunState"], f"{w['state']}")
check("wait_for_job rejects a bad run id", "error" in json.loads(wait_for_job("not-a-run-id")))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
