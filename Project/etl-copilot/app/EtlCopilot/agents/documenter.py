"""Documenter: writes a dataset's data dictionary and quality report after a passing run.

The split is deliberate: tools/docs.py computes every figure from AWS; this agent only
writes prose - what each column means and how the script that ran derived it - from the
approved spec and that exact script. It never sees the data, so it cannot leak it, and
the rendered documents are scanned for personal data before they are saved.
"""
import json
import logging
import time
from typing import List

from pydantic import BaseModel, Field
from strands import Agent, tool

import narration
from model.load import load_model
from tools.docs import DocsError, gather_facts, render_dictionary, render_quality, save_docs
from tools.drafts import read_draft
from tools.export import export_csv
from tools.specs import SpecNotFound

progress = logging.getLogger("etl_copilot")

SYSTEM_PROMPT = """You are the Documenter in ETL Copilot, an assistant for data engineers.
You write the prose of a dataset's data dictionary: what the clean table is, and for every
column how it was derived from the raw CSV. You are given the approved spec (the contract)
and the exact Python script that produced the table. Describe what the SCRIPT does; where
the spec and script differ, the script is what happened - say so in a caveat.

Rules:
- Never state counts, percentages, row numbers or statistics: those are computed by code
  and shown next to your text. Formats and units are fine (YYYY-MM, SHA-256, grams, UTC).
- Never include personal data or realistic example values of it (emails, phones, names).
- One entry per output column, including the partition column, using the exact column name.
- derivation: 1-2 plain sentences an analyst can trust: the raw source, the parsing, and
  what becomes null.
- caveats: things a downstream user must know - values treated as placeholders, guessed
  conventions, rounding, columns dropped or masked, joins. Empty list if there are none.
"""


class ColumnDoc(BaseModel):
    name: str = Field(description="exact output column name")
    derivation: str = Field(description="how the script derives it from the raw CSV, 1-2 sentences")


class DatasetDoc(BaseModel):
    overview: str = Field(description="2-3 sentences: what the table is and where it comes from")
    columns: List[ColumnDoc]
    caveats: List[str]


def write_prose(facts: dict) -> dict:
    spec = facts["spec"]
    script = read_draft(facts["run"]["draft_id"])
    names = [c["name"] for c in facts["columns"]]
    prompt = (f"Dataset: {facts['dataset']}\nOutput columns, in order: {names}\n\n"
              f"Approved spec:\n{json.dumps(spec, indent=1)}\n\n"
              f"Script that produced the table (draft {facts['run']['draft_id']}):\n```python\n{script}\n```")
    agent = Agent(model=load_model(), system_prompt=SYSTEM_PROMPT, callback_handler=None)
    doc = agent(prompt, structured_output_model=DatasetDoc).structured_output
    columns = {c.name: c.derivation for c in doc.columns if c.name in names}
    missing = [n for n in names if n not in columns]
    if missing:
        progress.info("documenter: no derivation for %s", missing)
    return {"overview": doc.overview, "columns": columns, "caveats": doc.caveats}


def write_docs(dataset: str) -> dict:
    """Plain function (no Supervisor) so tests can call it directly."""
    started = time.time()
    facts = gather_facts(dataset)
    prose = write_prose(facts)
    paths = save_docs(dataset, render_dictionary(facts, prose), render_quality(facts))
    progress.info("documenter: %s documented in %.0fs", dataset, time.time() - started)
    return {"dataset": dataset, "run_id": facts["run"]["run_id"], "passed": facts["validation"]["passed"], **paths}


@tool
def documenter(dataset: str) -> str:
    """Write the data dictionary and quality report for a dataset whose latest run passed.

    Use after the execution agent reports a passing validation, or when the engineer asks
    for documentation. Returns the S3 locations of data_dictionary.md, quality_report.md and a CSV
    copy of the clean table (clean_csv),
    or the reason it cannot document the dataset yet.

    Args:
        dataset: the dataset name, i.e. its folder under landing/ (e.g. "orders").
    """
    progress.info("documenter: %s started (about 1 minute)", dataset)
    narration.activity("documenter", f"Documenter is writing the data dictionary and quality report for {dataset}")
    try:
        result = write_docs(dataset)
        try:  # the CSV copy is a convenience: failing it must not hide the docs
            narration.activity("documenter", f"Documenter is exporting {dataset} as CSV")
            result["clean_csv"] = export_csv(dataset)
        except Exception as e:
            progress.info("documenter: CSV export failed: %s", e)
        narration.post(narration.docs_step(result))
        return json.dumps(result)
    except (DocsError, SpecNotFound, ValueError) as e:
        return f"NOT_DOCUMENTED: {e}"
