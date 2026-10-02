"""AWS credentials for every tool: assume EtlCopilotExecutionRole (Phase 2).

No tool ever uses the caller's own identity. Locally that identity is your admin SSO
login; deployed it is the AgentCore runtime role. Either way, the only permissions a
tool has are the ones in EtlCopilotExecutionPolicy.
"""
import time

import boto3

from config import EXECUTION_ROLE_ARN, REGION

SESSION_NAME = "etl-copilot-tools"  # shows up as the "user name" in CloudTrail
REFRESH_MARGIN_S = 300              # re-assume 5 minutes before the credentials expire

_cache = {"session": None, "expires": 0.0}


def execution_session() -> boto3.Session:
    """Return a boto3 Session signed in as EtlCopilotExecutionRole, re-assuming near expiry."""
    if _cache["session"] is None or time.time() > _cache["expires"] - REFRESH_MARGIN_S:
        creds = boto3.client("sts", region_name=REGION).assume_role(
            RoleArn=EXECUTION_ROLE_ARN,
            RoleSessionName=SESSION_NAME,
            DurationSeconds=3600,  # SSO -> role is "role chaining", which AWS caps at 1 hour
        )["Credentials"]
        _cache["session"] = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=REGION,
        )
        _cache["expires"] = creds["Expiration"].timestamp()
    return _cache["session"]
