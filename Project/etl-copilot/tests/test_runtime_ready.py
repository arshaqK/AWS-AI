"""Step 4.4.1 gate: what the deployed runtime needs, checked before the deploy.

  A. Approvals are durable: they live in S3, so a restart (a fresh process, a new microVM)
     still sees them; one approval still allows exactly one run.
  B. Expired, stale or malformed approvals run nothing.
  C. (IAM) The Execution role - the one the models' tools use - cannot read, write or
     delete approvals.
  D. (IAM) The runtime role's extra policy allows exactly: assuming the Execution role and
     the approvals/ objects; nothing else.
  E. agentcore.json declares that policy and a 2 h idle timeout.

Writes and removes a few test objects under s3://etl-copilot-ak/approvals/ with your own
login; creates nothing else. Usage (from app/EtlCopilot/):
    uv run python ../../tests/test_runtime_ready.py
"""
import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(os.getenv("ETL_COPILOT_APP", ROOT / "app" / "EtlCopilot"))
sys.path.insert(0, str(APP_DIR))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

import approvals  # noqa: E402
from config import APPROVALS_PREFIX, BUCKET, EXECUTION_ROLE_ARN, REGION  # noqa: E402

results = []
DRAFT, OLD, STALE = "feedface0001", "feedface0002", "feedface0003"


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


s3 = boto3.client("s3", region_name=REGION)
for d in (DRAFT, OLD, STALE):  # start clean
    s3.delete_object(Bucket=BUCKET, Key=f"{APPROVALS_PREFIX}{d}.json")

print("--- A. approvals survive a restart ---")
msg = approvals.handle_human_message(f"APPROVE {DRAFT}")
check("APPROVE is recorded in S3", s3.head_object(Bucket=BUCKET, Key=f"{APPROVALS_PREFIX}{DRAFT}.json") is not None
      and "APPROVED" in msg)
approvals = importlib.reload(approvals)  # a fresh process: no in-memory state, new client
check("a restarted process still sees it", DRAFT in approvals.pending())
check("it allows one run", approvals.consume(DRAFT) is True)
check("and only one", approvals.consume(DRAFT) is False and DRAFT not in approvals.pending())

print("--- B. expired, stale or malformed approvals run nothing ---")
old = (datetime.now(timezone.utc) - approvals.APPROVAL_TTL - timedelta(minutes=1)).isoformat()
s3.put_object(Bucket=BUCKET, Key=f"{APPROVALS_PREFIX}{OLD}.json",
              Body=json.dumps({"draft_id": OLD, "approved_at": old}).encode())
check("an approval older than 24 h is refused", approvals.consume(OLD) is False)
check("and removed", OLD not in approvals.pending())
approvals.approve(STALE)
etag = s3.head_object(Bucket=BUCKET, Key=f"{APPROVALS_PREFIX}{STALE}.json")["ETag"]
approvals.approve(STALE)  # someone else touched it after we read it
try:
    s3.delete_object(Bucket=BUCKET, Key=f"{APPROVALS_PREFIX}{STALE}.json", IfMatch=etag)
    stale_refused = False
except ClientError as e:
    stale_refused = e.response["Error"]["Code"] in ("PreconditionFailed", "412")
check("S3 refuses to delete an approval that changed since it was read (two consumers cannot both run)",
      stale_refused)
approvals.consume(STALE)
check("a malformed id is never consumed", approvals.consume("../landing/x") is False)
try:
    approvals.approve("../landing/x")
    check("a malformed id is never written", False)
except ValueError:
    check("a malformed id is never written", True)


class BrokenS3:
    def put_object(self, **kwargs):
        raise RuntimeError("S3 unavailable")


approvals._client = BrokenS3()
msg = approvals.handle_human_message("APPROVE 000000000001")
check("an S3 failure on APPROVE is reported, not raised", "could not be recorded" in msg, msg[:70])
approvals._client = None

print("--- C. the models' tools cannot touch approvals ---")
iam = boto3.client("iam")
arn = f"arn:aws:s3:::{BUCKET}/{APPROVALS_PREFIX}{DRAFT}.json"
for action in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject"):
    decision = iam.simulate_principal_policy(PolicySourceArn=EXECUTION_ROLE_ARN, ActionNames=[action],
                                             ResourceArns=[arn])["EvaluationResults"][0]["EvalDecision"]
    check(f"Execution role: {action} on approvals/ denied", decision != "allowed", decision)

print("--- D. the runtime role's extra policy is exactly what it needs ---")
policy = (APP_DIR / "iam" / "runtime-policy.json").read_text()
cases = [
    ("sts:AssumeRole", EXECUTION_ROLE_ARN, None, True),
    ("sts:AssumeRole", EXECUTION_ROLE_ARN.replace("EtlCopilotExecutionRole", "OrganizationAccountAccessRole"), None, False),
    ("s3:PutObject", arn, None, True),
    ("s3:DeleteObject", arn, None, True),
    ("s3:PutObject", f"arn:aws:s3:::{BUCKET}/landing/orders/x.csv", None, False),
    ("s3:GetObject", f"arn:aws:s3:::{BUCKET}/scripts/etl-copilot-job.py", None, False),
    ("s3:ListBucket", f"arn:aws:s3:::{BUCKET}", "approvals/", True),
    ("s3:ListBucket", f"arn:aws:s3:::{BUCKET}", "landing/", False),
]
for action, resource, prefix, expected in cases:
    kwargs = {"ContextEntries": [{"ContextKeyName": "s3:prefix", "ContextKeyValues": [prefix],
                                  "ContextKeyType": "string"}]} if prefix else {}
    decision = iam.simulate_custom_policy(PolicyInputList=[policy], ActionNames=[action], ResourceArns=[resource],
                                          **kwargs)["EvaluationResults"][0]["EvalDecision"]
    shown = resource.split(":")[-1] + (f" prefix={prefix}" if prefix else "")
    check(f"runtime policy: {action} on {shown} {'allowed' if expected else 'denied'}",
          (decision == "allowed") == expected, decision)

print("--- E. agentcore.json ---")
runtime = json.loads((ROOT / "agentcore" / "agentcore.json").read_text())["runtimes"][0]
check("the runtime declares the policy", runtime.get("additionalPolicies") == ["iam/runtime-policy.json"])
life = runtime.get("lifecycleConfiguration", {})
check("idle timeout 2 h, max lifetime 8 h", (life.get("idleRuntimeSessionTimeout"), life.get("maxLifetime")) == (7200, 28800))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
