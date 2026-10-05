"""Execution Agent: the only agent that can start the Glue job.

Its prompt tells it to stop on a refusal, but the real guarantee is in code:
start_glue_job refuses any draft without a human approval (approvals.py), whatever
this agent is told or decides. The draft's dataset is read from the draft itself, and
the validation tool is bound to that dataset, so the agent cannot validate a run
against the wrong spec.
"""
import logging
import time

from strands import Agent, tool

import narration
from aws_session import execution_session
from config import BUCKET, JOB_NAME
from model.load import load_model
from tools.drafts import DRAFT_ID_RE
from tools.execution import (LAST_VALIDATION, TERMINAL_STATES, draft_dataset, make_validation_tool,
                             start_glue_job, validate_output, wait_for_job)
from tools.runlog import RUNS_PREFIX, record_run

progress = logging.getLogger("etl_copilot")

SYSTEM_PROMPT = """You are the Execution Agent in ETL Copilot. You are the only agent that can
run the Glue ETL job, and you only run drafts the engineer approved.

## How to work
1. Call start_glue_job once, with the draft id given in the task.
2. If it returns REFUSED or BUSY, stop. Report the message word for word. Never retry,
   never try a different draft id, never say a draft is approved when the tool says not.
3. If it started, call wait_for_job with the run id.
4. If the final state is not SUCCEEDED, stop and report the state and the error message
   word for word: it is the ETL script's own error and the script writer needs it exactly.
5. If it SUCCEEDED, call run_athena_validation.

## Reply format
Plain text exactly as below: no markdown, no bold, no extra commentary.
DRAFT_ID: <id>
DATASET: <dataset>
RUN: <run id, or "not started">
STATE: <SUCCEEDED / FAILED / ... / REFUSED / BUSY>
ERROR: <the exact error or refusal message, or "none">
VALIDATION: <PASSED / FAILED / not run>
- <rule>: <passed/failed> - <detail>   (one line per rule, only if validation ran)
  examples: <for a failed rule, its "examples" values copied exactly>
INFO: <the info counts, if validation ran>
"""


@tool
def execution_agent(draft_id: str, note: str = "") -> str:
    """Run an approved ETL draft on Glue, wait for it, and validate the cleaned output.

    Only use after the engineer approved the draft. Returns the run state, the exact error
    text if the job failed, and each validation rule's result. If the draft has no
    approval, it reports REFUSED and runs nothing.

    Args:
        draft_id: the 12-character id of the approved draft.
        note: optional context for the run (it cannot grant approval).
    """
    if not DRAFT_ID_RE.match(draft_id or ""):
        return f"DRAFT_ID: {draft_id}\nSTATE: REFUSED\nERROR: {draft_id!r} is not a draft id"
    try:
        dataset = draft_dataset(draft_id)
    except Exception as e:
        return f"DRAFT_ID: {draft_id}\nSTATE: REFUSED\nERROR: cannot read draft {draft_id}: {e}"
    agent = Agent(
        model=load_model(),
        system_prompt=SYSTEM_PROMPT,
        tools=[start_glue_job, wait_for_job, make_validation_tool(dataset)],
        callback_handler=None,  # no token printing (crashes on Windows cp1252)
    )
    progress.info("execution_agent: %s / %s started (a Glue run takes about 2-4 minutes)", draft_id, dataset)
    task = f"Run draft {draft_id} (dataset {dataset})." + (f" Note: {note}" if note else "")
    started, before = time.time(), _latest_run_id()
    narration.speaking("execution")
    narration.activity("execution", f"Execution is starting the Glue job for draft {draft_id}")
    try:
        reply = str(agent(task))
    finally:
        narration.speaking("supervisor")
        narration.activity("supervisor", "Supervisor is reviewing the run")
    return reply + _log_run(dataset, draft_id, before, started)


def _latest_run_id() -> str:
    runs = execution_session().client("glue").get_job_runs(JobName=JOB_NAME, MaxResults=1)["JobRuns"]
    return runs[0]["Id"] if runs else ""


def _log_run(dataset: str, draft_id: str, before: str, started: float) -> str:
    """Record the run this call started, from what Glue and Athena report (not the agent's words)."""
    try:
        glue = execution_session().client("glue")
        run = glue.get_job_runs(JobName=JOB_NAME, MaxResults=1)["JobRuns"][0]
        if run["Id"] == before:
            return ""  # refused or busy: nothing ran, nothing to record
        if run["JobRunState"] not in TERMINAL_STATES:  # the agent stopped before the run ended
            wait_for_job(run["Id"])
            run = glue.get_job_run(JobName=JOB_NAME, RunId=run["Id"])["JobRun"]
        validation = None
        if run["JobRunState"] == "SUCCEEDED":
            at, result = LAST_VALIDATION.get(dataset, (0.0, None))
            validation = result if at >= started else validate_output(dataset)
        record = record_run(dataset, draft_id, run["Id"], validation)
        narration.post(narration.run_step(run, draft_id))
        if validation:
            narration.post(narration.validation_step(validation, record["attempt"]))
        progress.info("run log: %s attempt %d %s", dataset, record["attempt"], record["outcome"])
        return (f"\nRUN_LOG: s3://{BUCKET}/{RUNS_PREFIX}{dataset}/{run['Id']}.json "
                f"(attempt {record['attempt']}, {record['outcome']})")
    except Exception as e:  # a logging failure must never hide the run's result
        progress.info("run log: could not record run: %s", e)
        return f"\nRUN_LOG: not recorded ({e})"
