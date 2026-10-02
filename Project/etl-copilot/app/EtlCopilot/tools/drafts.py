"""Save LLM-written ETL drafts to S3 after static checks, and read them back.

A draft's id is the first 12 hex chars of the SHA-256 of its text, so the id names the
exact content: step 3.3 re-hashes the file before running it and refuses on mismatch.

A draft belongs to exactly one dataset, read from the script itself: it must contain that
dataset's landing and staging URIs as literal strings, and no other s3:// location. The
dataset is therefore covered by the same hash - it cannot be changed without a new id.
"""
import ast
import hashlib
import json
import logging
import re
from typing import List, Optional

from strands import tool

from aws_session import execution_session
from config import BUCKET, CLEAN_DB, DRAFTS_PREFIX
from tools.specs import paths

progress = logging.getLogger("etl_copilot")

MAX_SCRIPT_BYTES = 60_000
DRAFT_ID_RE = re.compile(r"^[0-9a-f]{12}$")

# Glue Python shell with "analytics" libraries: stdlib + these. Anything else fails at run time.
ALLOWED_IMPORTS = {
    "awswrangler", "pandas", "numpy",
    "collections", "datetime", "decimal", "functools", "hashlib", "itertools", "json",
    "logging", "math", "re", "string", "sys", "typing", "unicodedata",
}


def draft_id_for(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()[:12]


def _string_constants(tree: ast.AST) -> set:
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def dataset_of_script(script: str) -> Optional[str]:
    """The dataset a script reads from: the single landing/<dataset>/ URI it contains."""
    try:
        constants = _string_constants(ast.parse(script))
    except SyntaxError:
        return None
    root = f"s3://{BUCKET}/landing/"
    found = {c[len(root):-1] for c in constants if c.startswith(root) and c.endswith("/")}
    return found.pop() if len(found) == 1 else None


def check_script(script: str, dataset: str) -> List[str]:
    """Static checks that catch, before any money is spent, what would fail on Glue
    or would read or write outside this dataset's folders and table."""
    if len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
        return [f"script is larger than {MAX_SCRIPT_BYTES} bytes"]
    try:
        tree = ast.parse(script, feature_version=(3, 9))
    except SyntaxError as e:
        return [f"not valid Python 3.9 syntax (Glue Python shell runs 3.9): line {e.lineno}: {e.msg}"]

    problems = []
    where = paths(dataset)
    constants = _string_constants(tree)
    for needed, what in ((where["landing_uri"], "read path"), (where["staging_uri"], "write path"),
                         (CLEAN_DB, "database"), (where["clean_table"], "table")):
        if needed not in constants:
            problems.append(f"the {what} must appear as the exact literal string {needed!r}")
    allowed = {where["landing_uri"], where["staging_uri"]}
    problems += [f"s3 location {c!r} is outside this dataset; only {sorted(allowed)} are allowed"
                 for c in constants if c.startswith("s3://") and c not in allowed]
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ("clean", "main"):
        if name not in functions:
            problems.append(f"missing top-level function {name}()")
    if not any(isinstance(n, ast.If) and "__name__" in ast.unparse(n.test) for n in tree.body):
        problems.append('missing `if __name__ == "__main__": main()` (Glue would run nothing)')

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                problems.append("relative imports are not allowed")
                continue
            roots = [(node.module or "").split(".")[0]]
        else:
            roots = []
        problems += [f"import of {r!r} is not allowed; allowed: {sorted(ALLOWED_IMPORTS)}"
                     for r in roots if r not in ALLOWED_IMPORTS]

        # `X | None` hints parse on 3.9 but raise TypeError when the def is executed
        annotations = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations = [a.annotation for a in node.args.args + node.args.kwonlyargs] + [node.returns]
        elif isinstance(node, ast.AnnAssign):
            annotations = [node.annotation]
        for ann in annotations:
            if ann is not None and any(isinstance(x, ast.BinOp) and isinstance(x.op, ast.BitOr)
                                       for x in ast.walk(ann)):
                problems.append(f"line {ann.lineno}: `X | Y` type hints fail on Python 3.9; use Optional/Union")
    return sorted(set(problems))


def save_draft(dataset: str, script: str) -> dict:
    """Plain function (no LLM) so tests can call it directly."""
    problems = check_script(script, dataset)
    if problems:
        progress.info("save_draft_script: rejected (%d problems), the script writer will fix them", len(problems))
        return {"saved": False, "problems": problems}
    draft_id = draft_id_for(script)
    key = f"{DRAFTS_PREFIX}{draft_id}.py"
    execution_session().client("s3").put_object(
        Bucket=BUCKET, Key=key, Body=script.encode("utf-8"), ContentType="text/x-python")
    progress.info("save_draft_script: saved draft %s", draft_id)
    return {"saved": True, "draft_id": draft_id, "dataset": dataset, "s3_uri": f"s3://{BUCKET}/{key}",
            "lines": script.count("\n") + 1}


def read_draft(draft_id: str) -> str:
    if not DRAFT_ID_RE.match(draft_id or ""):
        raise ValueError(f"not a draft id: {draft_id!r}")
    body = execution_session().client("s3").get_object(
        Bucket=BUCKET, Key=f"{DRAFTS_PREFIX}{draft_id}.py")["Body"].read()
    return body.decode("utf-8")


def make_save_draft_tool(dataset: str):
    """save_draft_script bound to one dataset, for the Script Writer working on it."""

    @tool
    def save_draft_script(script: str) -> str:
        """Check an ETL script and, if it passes, save it as a new draft.

        Checks: Python 3.9 syntax, top-level clean() and main(), a __main__ guard, only
        allowed imports, and this dataset's exact read/write paths and table as literal
        strings. Returns JSON: {"saved": true, "draft_id": ...} or
        {"saved": false, "problems": [...]} - fix every problem and call again.

        Args:
            script: the complete Python source of the Glue Python shell job.
        """
        return json.dumps(save_draft(dataset, script))

    return save_draft_script


@tool
def read_draft_script(draft_id: str) -> str:
    """Return the full source of an earlier draft, to revise it.

    Args:
        draft_id: the 12-character id of the draft.
    """
    try:
        return read_draft(draft_id)
    except Exception as e:  # unknown id, missing object: tell the model, don't crash the agent
        return f"ERROR: could not read draft {draft_id!r}: {e}"
