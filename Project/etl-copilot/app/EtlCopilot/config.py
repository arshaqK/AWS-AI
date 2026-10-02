"""Every shared AWS name ETL Copilot touches, in one place.

Tools read these constants instead of taking names as parameters, so the model can
never point a tool at a different job, workgroup or bucket. Everything specific to one
dataset (its tables, folders, columns and rules) lives in that dataset's approved spec;
see tools/specs.py.
"""
import os

REGION = os.getenv("AWS_REGION", "us-west-2")
ACCOUNT_ID = "481719141347"

# S3
BUCKET = "etl-copilot-ak"
LANDING_ROOT = "landing/"                           # one folder per dataset: landing/<dataset>/
STAGING_ROOT = "staging/"                           # one folder per dataset: staging/<dataset>/
JOB_SCRIPT_KEY = "scripts/etl-copilot-job.py"       # the script the Glue job actually runs
DRAFTS_PREFIX = "scripts/drafts/"                   # LLM script drafts, named <draft_id>.py
CONTRACTS_PREFIX = "scripts/contracts/"             # approved dataset specs, <dataset>.json
SPEC_DRAFTS_PREFIX = "scripts/contracts/drafts/"    # proposed dataset specs, <spec_id>.json
REPORTS_PREFIX = "reports/"

# Glue
JOB_NAME = "etl-copilot-job"
RAW_DB = "etl_copilot_raw"                          # crawler tables, raw_<dataset>
CLEAN_DB = "etl_copilot_clean"                      # cleaned tables, <dataset>

# Athena
WORKGROUP = "etl-copilot-wg"

# IAM: every tool call runs as this role (Phase 2), never as the caller's own identity
EXECUTION_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/EtlCopilotExecutionRole"
