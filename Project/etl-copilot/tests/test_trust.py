"""Step 4.4.2 gate: only ETL Copilot's own callers can become EtlCopilotExecutionRole.

  A. The live trust policy is exactly iam/execution-role-trust.json: it trusts the account
     only through an aws:PrincipalArn condition (your SSO role and this stack's roles).
  B. Your SSO login can still assume it (local dev and the tests keep working).
  C. The tools' own session cannot re-assume it (a role not on the list is refused).

Read-only apart from the role sessions it opens. Usage (from app/EtlCopilot/):
    uv run python ../../tests/test_trust.py
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(os.getenv("ETL_COPILOT_APP", ROOT / "app" / "EtlCopilot"))))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from config import EXECUTION_ROLE_ARN, REGION  # noqa: E402

results = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


print("--- A. the trust policy ---")
live = boto3.client("iam").get_role(RoleName=EXECUTION_ROLE_ARN.split("/")[-1])["Role"]["AssumeRolePolicyDocument"]
wanted = json.loads((ROOT / "iam" / "execution-role-trust.json").read_text())
check("live trust policy matches iam/execution-role-trust.json", live == wanted)
statements = live["Statement"]
check("every Allow is limited by aws:PrincipalArn",
      all("aws:PrincipalArn" in json.dumps(s.get("Condition", {})) for s in statements if s["Effect"] == "Allow"))
allowed = [a for s in statements for a in s.get("Condition", {}).get("ArnLike", {}).get("aws:PrincipalArn", [])]
RUNTIME_ROLE = "arn:aws:iam::481719141347:role/AgentCore-EtlCopilot-dev-ApplicationAgentEtlCopilot-ItNUmMjqGOk1"
SSO_ROLE = ("arn:aws:iam::481719141347:role/aws-reserved/sso.amazonaws.com/"
            "AWSReservedSSO_AdministratorAccess_597a794977fc897e")
check("exactly two callers: your SSO role and the deployed runtime role (no wildcards)",
      sorted(allowed) == sorted([SSO_ROLE, RUNTIME_ROLE]), str(allowed))
role = boto3.client("iam").get_role(RoleName=RUNTIME_ROLE.split("/")[-1])["Role"]
check("the runtime role is the one AgentCore runs as",
      "bedrock-agentcore.amazonaws.com" in json.dumps(role["AssumeRolePolicyDocument"]))

print("--- B. your login can still assume it ---")
sts = boto3.client("sts", region_name=REGION)
try:
    creds = sts.assume_role(RoleArn=EXECUTION_ROLE_ARN, RoleSessionName="trust-check")["Credentials"]
    check("SSO login -> Execution role", True)
except ClientError as e:
    creds = None
    check("SSO login -> Execution role", False, e.response["Error"]["Code"])

print("--- C. a role not on the list is refused ---")
if creds:
    chained = boto3.client("sts", region_name=REGION, aws_access_key_id=creds["AccessKeyId"],
                           aws_secret_access_key=creds["SecretAccessKey"], aws_session_token=creds["SessionToken"])
    try:
        chained.assume_role(RoleArn=EXECUTION_ROLE_ARN, RoleSessionName="trust-check-2")
        check("Execution role session -> Execution role refused", False, "it was allowed")
    except ClientError as e:
        check("Execution role session -> Execution role refused", e.response["Error"]["Code"] == "AccessDenied",
              e.response["Error"]["Code"])

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
