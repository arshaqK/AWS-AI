"""Script Writer: drafts (and revises) the Glue Python shell ETL for one dataset.

The prompt is built from the dataset's approved spec - the same spec the validation rules
check - so the prompt and the checks can never disagree. Without an approved spec the
Script Writer refuses in code, before any model runs. The golden script is deliberately
NOT shown to the model: the drafts must come from the profile and the spec.
"""
import json
import logging
import time

from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from config import CLEAN_DB
from model.load import MODEL_ID, REGION
from tools.drafts import ALLOWED_IMPORTS, make_save_draft_tool, read_draft_script
from tools.profile import get_dataset_profile
from tools.specs import SpecNotFound, load_spec, paths

progress = logging.getLogger("etl_copilot")

MAX_OUTPUT_TOKENS = 16000  # a full script travels as one tool argument

# Rules that hold for every dataset. Dataset-specific rules come from the spec.
GENERAL_PROMPT = """You are the Script Writer in ETL Copilot, an assistant for data engineers.
You write the AWS Glue Python shell job that turns one raw vendor CSV into a clean table.
This task is for the dataset "{dataset}".

## How to work
1. Call get_dataset_profile with dataset="{dataset}" and study it: null spellings, value
   shapes (formats), and the distinct values of low-cardinality columns (spelling variants).
2. If the task asks you to revise an earlier draft, call read_draft_script with its id
   and fix exactly what the task reports (a failed rule or a job error), keeping the rest.
3. Write the complete script and call save_draft_script. If it returns problems, fix
   every one and call it again until saved is true.
4. Reply in the format at the end. Never paste the script into your reply; it is in S3.

## Runtime
- Glue Python shell, Python 3.9: no `match`, no `X | Y` type hints (use Optional/Union).
- Allowed imports only: {imports}.

## Structure
- clean(raw) -> DataFrame: pure pandas, no I/O. raw is the CSV read as all-text
  (empty cells are "", never NaN). Returns exactly the output columns{partition_clause}.
  Print a short summary (rows in/out, duplicates removed, nulls per key column).
- main(): read, clean, write. Import awswrangler inside main() only, so clean() can be
  tested without AWS. End the file with `if __name__ == "__main__": main()`.
- Write these locations as literal strings, exactly as shown (they are checked):
  Read:  wr.s3.read_csv("{landing_uri}", dtype=str, keep_default_na=False,
         na_filter=False, encoding="utf-8")
  Write: wr.s3.to_parquet(df=..., path="{staging_uri}", dataset=True,
         mode="overwrite",{partition_arg} database="{clean_db}",
         table="{clean_table}", dtype={dtype})

## Output contract (exact names, order and types)
{columns}
{partition_line}
## Cleaning rules (every dataset)
- In every column, trim whitespace and treat '', 'N/A', 'null', '-' (any case) as null.
- Remove exact duplicate rows. If the key ({key}) still repeats with different content,
  raise ValueError listing the keys: never pick one silently.
- Never drop any other row. A null-like value becomes null. A value that is invalid
  under a convention the engineer stated (e.g. a placeholder date) becomes null.
  Any other value your parser does not recognise must raise ValueError naming the
  column and the value - a loud failure is fixable, a silent guess is not.

## Cleaning rules for {dataset} (from its approved spec)
{cleaning_rules}
{pii}
## Ambiguity
If the data allows two readings and the task does not settle it (e.g. 03/04/2025),
use the reading the data proves (a slash date whose first part is above 12 proves
DD/MM) and list it under Assumptions. Engineer conventions in the task always win.

## Revising after a failure
A failed rule shows example offending values. Fix the cause behind those exact values,
never the rule's whole range: if 1900-01-01 breaks a date rule, handle the value
1900-01-01 (e.g. as a "unknown date" placeholder), do NOT null every date before the
rule's limit - that would silently erase real data the rule was never about.
A fix the engineer did not state is a guess: list it under Assumptions starting with
"NEEDS CONFIRMATION:", naming the column, the exact value(s) and what you did with them,
so the engineer can confirm or reject it when approving the draft.

## Reply format
DRAFT_ID: <id from save_draft_script>
What the script does:
- <one bullet per quirk handled, in plain English, with the column name>
Assumptions:
- <anything not stated by the engineer, or "none">
- NEEDS CONFIRMATION: <column>: <exact value(s)> -> <what the script does>  (one per guessed fix)
If this is a revision, add "Changed since <previous id>:" with what you changed and why.
"""


def build_prompt(dataset: str, spec: dict) -> str:
    where = paths(dataset)
    part = spec.get("partition")
    columns = "\n".join(f"  {i}. {c['name']}  {c['type']}" for i, c in enumerate(spec["columns"], 1))
    non_string = {c["name"]: c["type"] for c in spec["columns"] if c["type"] != "string"}
    pii = ""
    if spec.get("pii"):
        lines = []
        for item in spec["pii"]:
            if item["treatment"] == "drop":
                lines.append(f"- {item['column']}: drop it" + (f" ({item['note']})" if item.get("note") else ""))
            else:
                lines.append(f"- {item['column']}: {item['treatment']} into {item['output']}"
                             + (f" ({item['note']})" if item.get("note") else ""))
        pii = "\n## PII (never write a raw value to the output)\n" + "\n".join(lines) + "\n"
    return GENERAL_PROMPT.format(
        dataset=dataset,
        imports=", ".join(sorted(ALLOWED_IMPORTS)),
        partition_clause=f" plus {part['name']}" if part else "",
        landing_uri=where["landing_uri"],
        staging_uri=where["staging_uri"],
        partition_arg=f' partition_cols=["{part["name"]}"],' if part else "",
        clean_db=CLEAN_DB,
        clean_table=where["clean_table"],
        dtype=json.dumps(non_string),
        columns=columns,
        partition_line=f"  partition: {part['name']} string  ({part['rule']})\n" if part else "",
        key=", ".join(spec["primary_key"]),
        cleaning_rules="\n".join(f"- {rule}" for rule in spec["cleaning_rules"]),
        pii=pii,
    )


def _model() -> BedrockModel:
    return BedrockModel(model_id=MODEL_ID, region_name=REGION, max_tokens=MAX_OUTPUT_TOKENS)


@tool
def script_writer(dataset: str, task: str) -> str:
    """Draft or revise the ETL script for one dataset, and save it as a draft.

    Only works for a dataset with an approved spec; otherwise it refuses and says so.
    The Script Writer profiles the raw table itself. Put in `task` everything it cannot
    learn from the data: the engineer's conventions (e.g. "slash dates are DD/MM/YYYY")
    and, for a revision, the previous draft id plus the exact failed rule or job error.
    Returns the new DRAFT_ID with a plain-English list of what the script does.

    Args:
        dataset: the dataset name, i.e. its folder under landing/ (e.g. "orders").
        task: what to write or fix, including conventions and failure details.
    """
    try:
        spec = load_spec(dataset)
    except SpecNotFound:
        progress.info("script_writer: REFUSED %s (no approved spec)", dataset)
        return (f"NO_SPEC: dataset {dataset!r} has no approved spec, so there is no contract to "
                "write a script against. Propose a spec with spec_writer and get the engineer's "
                "APPROVE SPEC first.")
    except ValueError as e:
        return f"ERROR: {e}"
    agent = Agent(
        model=_model(),
        system_prompt=build_prompt(dataset, spec),
        tools=[get_dataset_profile, read_draft_script, make_save_draft_tool(dataset)],
        callback_handler=None,  # no token printing (crashes on Windows cp1252)
    )
    progress.info("script_writer: %s started (usually 1-2 minutes)", dataset)
    started = time.time()
    reply = str(agent(task))
    progress.info("script_writer: finished in %.0fs", time.time() - started)
    return reply
