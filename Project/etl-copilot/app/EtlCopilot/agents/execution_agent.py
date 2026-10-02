"""Execution Agent: the only agent that can start the Glue job.

Its prompt tells it to stop on a refusal, but the real guarantee is in code:
start_glue_job refuses any draft without a human approval (approvals.py), whatever
this agent is told or decides. The draft's dataset is read from the draft itself, and
the validation tool is bound to that dataset, so the agent cannot validate a run
against the wrong spec.
"""
import logging

from strands import Agent, tool

from model.load import load_model
from tools.drafts import DRAFT_ID_RE
from tools.execution import draft_dataset, make_validation_tool, start_glue_job, wait_for_job

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
    return str(agent(task))
