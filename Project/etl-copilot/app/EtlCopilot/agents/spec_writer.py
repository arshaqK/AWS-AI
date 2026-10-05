"""Spec Writer: proposes the contract (spec) for a dataset, from its profile.

It can only SAVE PROPOSALS (scripts/contracts/drafts/<spec_id>.json). Nothing here can
make a proposal the dataset's contract: that takes the engineer's "APPROVE SPEC <id>",
read by the entrypoint (approvals.py), never by a model.
"""
import json
import logging
import time

from strands import Agent, tool
from strands.models.bedrock import BedrockModel

import approvals
import memory
import narration

from model.load import MODEL_ID, REGION
from tools.profile import get_dataset_profile
from tools.specs import (CORE_RULES, INFO_TYPES, PII_TREATMENTS, RULE_TYPES, SpecNotFound, load_spec,
                         raw_table_exists, read_spec_draft, save_spec_draft)


progress = logging.getLogger("etl_copilot")

MAX_OUTPUT_TOKENS = 12000

# A made-up example (not one of the project's datasets), so the model learns the shape
# of a spec without copying any real contract.
EXAMPLE = {
    "dataset": "payments",
    "description": "Daily card payments export from the payment provider.",
    "primary_key": ["payment_id"],
    "raw_key": ["payment_id"],
    "columns": [
        {"name": "payment_id", "type": "string", "description": "Provider payment id."},
        {"name": "paid_at", "type": "timestamp", "description": "When the payment settled, UTC."},
        {"name": "amount", "type": "decimal(12,2)", "description": "Amount in the payment currency."},
        {"name": "currency", "type": "string", "description": "ISO 4217 code."},
        {"name": "card_holder_sha256", "type": "string", "description": "SHA-256 of the card holder name."},
        {"name": "is_refund", "type": "boolean", "description": "True for refunds."},
    ],
    "partition": {"name": "paid_month", "type": "string",
                  "rule": "'YYYY-MM' from paid_at, or 'unknown' when paid_at is null"},
    "pii": [
        {"column": "card_holder", "treatment": "hash", "output": "card_holder_sha256",
         "note": "trim + upper-case, then SHA-256"},
        {"column": "card_last4", "treatment": "drop", "output": None, "note": "not needed downstream"},
    ],
    "cleaning_rules": [
        "paid_at: UTC timestamp. Parse every layout the profile shows; naive times are UTC.",
        "amount: decimal.Decimal quantized to 0.01; strip currency symbols and thousands separators.",
        "currency: ISO 4217, upper-case.",
        "is_refund: true for Y/yes/1/true (any case), false for N/no/0/false.",
    ],
    "rules": [
        {"id": "R4_valid_times", "type": "date_range", "column": "paid_at", "min": "2015-01-01", "max": "today"},
        {"id": "R5_currencies", "type": "matches", "column": "currency", "pattern": "^[A-Z]{3}$",
         "description": "a 3-letter upper-case ISO code"},
        {"id": "R6_pii_hashed", "type": "hashed", "columns": ["card_holder_sha256"]},
    ],
    "info": [
        {"name": "null_paid_at", "type": "null_count", "column": "paid_at"},
        {"name": "non_refunds", "type": "false_count", "column": "is_refund"},
    ],
}

SYSTEM_PROMPT = f"""You are the Spec Writer in ETL Copilot, an assistant for data engineers.
You propose the contract ("spec") for one raw CSV dataset: what its clean table must look
like and how it is checked. The engineer reviews and approves it; a Script Writer later
writes the ETL against it, and the validation rules are run from it.

## How to work
1. Call get_dataset_profile for the dataset and study every column: null spellings, value
   shapes, low-cardinality values, letter_tokens, sample rows. letter_tokens lists EVERY
   unit / marker / prefix that occurs in a column (e.g. kg, g, lb, lbs, oz): the sample rows
   are only 10 rows, so never rely on them alone - a cleaning rule must handle every token.
   "numeric" gives each numeric column's min, max and count of negatives over all rows.
2. If the task asks you to revise a proposal, call read_spec with its id and change only
   what the task asks.
3. Build the spec and call save_spec with it. If it returns problems, fix every one and
   call it again until saved is true.
4. Reply in the format at the end.

## What a spec contains (JSON)
- dataset: the dataset name you were given.
- description: one sentence on what the data is.
- primary_key: the output column(s) that identify a row; raw_key: the raw column behind each.
- columns: every output column, in order: name (snake_case), type, description.
  Types: string, int, bigint, double, boolean, date, timestamp, decimal(p,s).
  Choose the type the values really are (a price is decimal, a count is int, a yes/no is
  boolean), not the crawler's guess.
- partition: null, or one string column derived from a date (e.g. 'YYYY-MM'), with its rule.
- pii: every column holding personal data (emails, phones, names, addresses, free text that
  may contain them). treatment: {sorted(PII_TREATMENTS)}. "hash" writes SHA-256 hex into an
  output column named <column>_sha256; "drop" removes it; "redact" keeps the text with the
  personal data replaced. A raw PII column never appears in columns as-is.
- cleaning_rules: one instruction per column that needs work, starting with the column name,
  precise enough to code (formats to parse, how to normalise, what raises an error).
- rules: validation rules beyond the built-in {list(CORE_RULES)} (schema, no rows lost,
  unique key). Types: {sorted(RULE_TYPES)}. Ids R4_... onwards. Use date_range for every
  date column, allowed_values for every enum-like column, matches for codes with a format,
  and hashed for every hashed PII column.
- info: counts worth reporting that are not failures (types {sorted(INFO_TYPES)}), e.g. how
  many dates are null.

## Example (a different, made-up dataset - do not copy its columns)
{json.dumps(EXAMPLE, indent=1)}

## Guessing
When the profile does not settle a choice (a key, a type, whether a column is PII, a date
convention), make the safer choice and list it under NEEDS CONFIRMATION.

## Rules must fit the data you saw
A validation rule must accept every non-null value the profile shows, once cleaned. If the
data contains values a rule would reject (e.g. negative counts, which can be backorders or
refunds), do NOT add the rule: describe those values under NEEDS CONFIRMATION instead, so
the engineer decides whether they are valid. A rule that fails on day one is a bug.

## Reply format
SPEC_ID: <id from save_spec>
Dataset: <name> - <description>
Key: <primary key>
Columns:
- <name> <type> - <what it is / how it is derived>   (one line per column)
Partition: <name and rule, or none>
PII: <column -> treatment, one per line, or "none">
Rules: <id: what it checks, one per line>
NEEDS CONFIRMATION:
- <each guess, or "nothing">

The engineer reads the NEEDS CONFIRMATION bullets on an approval card: one guess per bullet,
in the form `<column>`: <the guess> (under 20 words), no reasoning or alternatives.
If this is a revision, end with "Changed since <previous spec id>:" and one bullet per change
(under 20 words each) - one for every change the engineer asked for.
"""


def _model() -> BedrockModel:
    return BedrockModel(model_id=MODEL_ID, region_name=REGION, max_tokens=MAX_OUTPUT_TOKENS)


def _make_tools(dataset: str, saved: list = None):
    @tool
    def save_spec(spec: dict) -> str:
        """Check a proposed spec and, if it passes, save it as a proposal.

        Returns JSON: {"saved": true, "spec_id": ...} or {"saved": false, "problems": [...]}
        - fix every problem and call again. The dataset field must be the one you were given.

        Args:
            spec: the complete spec as a JSON object.
        """
        if not isinstance(spec, dict) or spec.get("dataset") != dataset:
            return json.dumps({"saved": False, "problems": [f'"dataset" must be "{dataset}"']})
        result = save_spec_draft(spec)
        if result["saved"] and saved is not None:
            saved.append(result["spec_id"])
        progress.info("save_spec: %s", f"saved spec {result['spec_id']}" if result["saved"]
                      else f"rejected ({len(result['problems'])} problems)")
        return json.dumps(result)

    @tool
    def read_spec(spec_id: str) -> str:
        """Return an earlier spec proposal, to revise it.

        Args:
            spec_id: the 12-character id of the proposal.
        """
        try:
            return json.dumps(read_spec_draft(spec_id))
        except Exception as e:
            return f"ERROR: could not read spec {spec_id!r}: {e}"

    return [save_spec, read_spec]


@tool
def spec_writer(dataset: str, task: str) -> str:
    """Propose (or revise) the spec - the contract - for one dataset, from its profile.

    Use for a dataset that has no approved spec, or when the engineer asks to change one.
    Needs the dataset's raw table (CSV in landing/<dataset>/ and the crawler run).
    Returns SPEC_ID with a summary for the engineer to approve with APPROVE SPEC <id>.

    Args:
        dataset: the dataset name, i.e. its folder under landing/.
        task: what to propose or change, including anything the engineer said about the data.
    """
    try:
        if not raw_table_exists(dataset):
            return (f"NO_DATA: there is no raw table for {dataset!r}. Upload the CSV to "
                    f"landing/{dataset}/ and run the crawler first.")
    except ValueError as e:
        return f"ERROR: {e}"
    note = ""
    try:
        load_spec(dataset)
        note = (f"\n(Dataset {dataset} already has an approved spec; approving a new proposal "
                "replaces it.)")
    except SpecNotFound:
        pass
    saved: list = []
    agent = Agent(
        model=_model(),
        system_prompt=SYSTEM_PROMPT,
        tools=[get_dataset_profile, *_make_tools(dataset, saved)],
        callback_handler=None,  # no token printing (crashes on Windows cp1252)
    )
    progress.info("spec_writer: %s started (usually 1-2 minutes)", dataset)
    started = time.time()
    narration.speaking("spec")
    narration.activity("spec", f"Spec Writer is writing the contract for {dataset}")
    try:
        reply = str(agent(f'Dataset: "{dataset}". {task}' + memory.as_prompt(dataset)
                          + approvals.take_request("spec", dataset)))
    finally:
        narration.speaking("supervisor")
        narration.activity("supervisor", "Supervisor is preparing the contract for your review")
    progress.info("spec_writer: finished in %.0fs", time.time() - started)
    _narrate_gate(reply, saved)
    return reply + note


def _narrate_gate(reply: str, saved: list) -> None:
    """The spec gate, built from the proposal this call saved - not from the reply's wording."""
    match = narration.SPEC_ID_RE.search(reply)
    if not match or match.group(1) not in saved:
        return  # the reply names no proposal this call saved: no gate to show
    try:
        spec = read_spec_draft(match.group(1))
    except Exception:
        return
    points = [memory.clean_confirmation(p) for p in narration.section(reply, "NEEDS CONFIRMATION")]
    points = [p for p in points if p and p.lower() not in ("nothing", "none")]
    narration.post(narration.spec_gate(match.group(1), spec, points, narration.section(reply, "Changed since")))
