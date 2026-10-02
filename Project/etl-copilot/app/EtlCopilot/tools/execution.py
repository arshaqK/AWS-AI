"""The only tools that run the ETL job or check its output.

None of them takes a job name, S3 path or job arguments: those are fixed in config.py
and in the dataset's approved spec, so the model cannot point a tool anywhere else.
All calls run as EtlCopilotExecutionRole.
"""
import hashlib
import json
import logging
import re
import time

from botocore.exceptions import ClientError
from strands import tool

import approvals
from athena import AthenaQueryError, run_query
from aws_session import execution_session
from config import BUCKET, CLEAN_DB, DRAFTS_PREFIX, JOB_NAME, JOB_SCRIPT_KEY, RAW_DB
from tools.drafts import DRAFT_ID_RE, dataset_of_script
from tools.profile import NULL_TOKENS
from tools.specs import SpecNotFound, date_sql, expected_schema, ident, literal, load_spec, paths

JOB_POLL_INTERVAL_S = 15
JOB_TIMEOUT_S = 600
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "STOPPED", "TIMEOUT", "ERROR", "EXPIRED"}
ACTIVE_STATES = {"STARTING", "RUNNING", "STOPPING", "WAITING"}

progress = logging.getLogger("etl_copilot")
RUN_ID_RE = re.compile(r"^jr_[0-9a-f]{64}$")
HASH_RE = "^[0-9a-f]{64}$"
MAX_EXAMPLES = 5  # offending values returned per failed rule


def _run_in_progress(glue) -> bool:
    runs = glue.get_job_runs(JobName=JOB_NAME, MaxResults=5)["JobRuns"]
    return any(r["JobRunState"] in ACTIVE_STATES for r in runs)


def draft_dataset(draft_id: str) -> str:
    """The dataset a stored draft belongs to (read from the script, so hash-protected)."""
    body = execution_session().client("s3").get_object(
        Bucket=BUCKET, Key=f"{DRAFTS_PREFIX}{draft_id}.py")["Body"].read()
    dataset = dataset_of_script(body.decode("utf-8"))
    if dataset is None:
        raise ValueError(f"draft {draft_id} does not name exactly one landing/<dataset>/ folder")
    return dataset


@tool
def start_glue_job(draft_id: str) -> str:
    """Run an approved ETL draft on the Glue job.

    Refuses unless the engineer approved exactly this draft id; one approval allows one
    run. If it returns REFUSED or BUSY, report that to the engineer - do not retry and
    do not try another draft id. On success returns JSON with the run id for wait_for_job.

    Args:
        draft_id: the 12-character id of the draft the engineer approved.
    """
    if not DRAFT_ID_RE.match(draft_id or ""):
        return f"REFUSED: {draft_id!r} is not a draft id."
    glue = execution_session().client("glue")
    if _run_in_progress(glue):  # checked first, so a busy job does not use up the approval
        return "BUSY: a run of the ETL job is already in progress. Wait for it, then ask again."

    if not approvals.consume(draft_id):
        progress.info("start_glue_job: REFUSED %s (no human approval)", draft_id)
        return (f"REFUSED: draft {draft_id} has no human approval. "
                f"Ask the engineer to review it and reply: APPROVE {draft_id}")

    s3 = execution_session().client("s3")
    try:
        body = s3.get_object(Bucket=BUCKET, Key=f"{DRAFTS_PREFIX}{draft_id}.py")["Body"].read()
    except ClientError as e:
        return f"REFUSED: draft {draft_id} could not be read ({e.response['Error']['Code']})."
    if hashlib.sha256(body).hexdigest()[:12] != draft_id:
        return (f"REFUSED: draft {draft_id} changed after it was proposed. "
                "A new draft and a new approval are required.")
    dataset = dataset_of_script(body.decode("utf-8"))
    if dataset is None:
        return f"REFUSED: draft {draft_id} does not name exactly one landing/<dataset>/ folder."
    try:
        load_spec(dataset)
    except SpecNotFound:
        return f"REFUSED: dataset {dataset!r} has no approved spec, so its output cannot be validated."

    # Write the exact bytes that were just verified, so nothing can change in between.
    s3.put_object(Bucket=BUCKET, Key=JOB_SCRIPT_KEY, Body=body, ContentType="text/x-python")
    try:
        run_id = glue.start_job_run(JobName=JOB_NAME)["JobRunId"]
    except glue.exceptions.ConcurrentRunsExceededException:
        approvals.approve(draft_id)  # nothing ran, so give the approval back
        return "BUSY: a run of the ETL job started in the meantime. Wait for it, then ask again."
    progress.info("start_glue_job: draft %s (%s) started as %s", draft_id, dataset, run_id[:16] + "...")
    return json.dumps({"started": True, "draft_id": draft_id, "dataset": dataset, "run_id": run_id})


@tool
def wait_for_job(run_id: str) -> str:
    """Wait for a Glue job run to finish (up to 10 minutes) and report how it ended.

    Returns JSON with the final state, the error message if it failed (this is the
    ETL script's own exception text, e.g. an unrecognised value), and the run time.

    Args:
        run_id: the run id returned by start_glue_job, e.g. "jr_..." .
    """
    if not RUN_ID_RE.match(run_id or ""):
        return json.dumps({"error": f"not a Glue job run id: {run_id!r}"})
    glue = execution_session().client("glue")
    deadline = time.time() + JOB_TIMEOUT_S
    while True:
        run = glue.get_job_run(JobName=JOB_NAME, RunId=run_id)["JobRun"]
        state = run["JobRunState"]
        progress.info("wait_for_job: %s %s (%ss)", run_id[:16] + "...", state, run.get("ExecutionTime", 0))
        if state in TERMINAL_STATES:
            return json.dumps({
                "run_id": run_id,
                "state": state,
                "error_message": run.get("ErrorMessage"),
                "execution_time_s": run.get("ExecutionTime"),
            })
        if time.time() > deadline:
            return json.dumps({"run_id": run_id, "state": state,
                               "error_message": f"still {state} after {JOB_TIMEOUT_S}s; check again"})
        time.sleep(JOB_POLL_INTERVAL_S)


# ---------- validation: R1-R3 from the contract, the rest from the spec's rules ----------

def _key_sql(columns, raw: bool = False) -> str:
    """One expression per row for the (possibly composite) key."""
    parts = [f"trim(CAST({ident(c)} AS varchar))" if raw else ident(c) for c in columns]
    if len(parts) == 1:
        return parts[0]
    return "concat_ws('|', " + ", ".join(p if raw else f"CAST({p} AS varchar)" for p in parts) + ")"


def _rule_sql(rule: dict, table: str):
    """(SQL counting rows that break the rule, SQL listing example values or None, detail text)."""
    rtype = rule["type"]
    if rtype == "hashed":
        bad = " OR ".join(f"({ident(c)} IS NOT NULL AND NOT regexp_like({ident(c)}, {literal(HASH_RE)}))"
                          for c in rule["columns"])
        # never list examples: the offending values may be raw PII
        return f"count_if({bad})", None, f"values in {rule['columns']} that are not 64-char hex SHA-256"
    col = ident(rule["column"])
    if rtype == "date_range":
        cond = (f"{col} IS NOT NULL AND (CAST({col} AS date) < {date_sql(rule['min'])} "
                f"OR CAST({col} AS date) > {date_sql(rule['max'])})")
        detail = f"non-null {rule['column']} outside {rule['min']}..{rule['max']}"
    elif rtype == "allowed_values":
        values = ", ".join(literal(v) for v in rule["values"])
        cond = f"{col} IS NOT NULL AND CAST({col} AS varchar) NOT IN ({values})"
        detail = f"{rule['column']} not in {rule['values']}"
    else:  # matches
        cond = f"{col} IS NOT NULL AND NOT regexp_like(CAST({col} AS varchar), {literal(rule['pattern'])})"
        detail = f"{rule['column']} not {rule.get('description') or 'matching ' + rule['pattern']}"
    # no str.format on this SQL: regex patterns such as ^[A-Z]{2}$ contain braces
    examples = f"SELECT CAST({col} AS varchar) AS {col}, count(*) AS n FROM {table} WHERE {cond} GROUP BY 1"
    return f"count_if({cond})", examples, detail


def _info_sql(item: dict) -> str:
    col = ident(item["column"])
    return {"null_count": f"count_if({col} IS NULL)",
            "distinct_count": f"count(DISTINCT {col})",
            "false_count": f"count_if({col} = false)"}[item["type"]]


def validate_output(dataset: str) -> dict:
    """Plain function (no LLM) so tests can call it directly."""
    spec = load_spec(dataset)
    where = paths(dataset)
    table = where["clean_table"]
    rules = {}

    # R1: schema matches the contract exactly
    try:
        glue_table = execution_session().client("glue").get_table(DatabaseName=CLEAN_DB, Name=table)["Table"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityNotFoundException":
            raise
        rules["R1_schema"] = {"passed": False, "detail": f"table {CLEAN_DB}.{table} does not exist yet"}
        return {"dataset": dataset, "passed": False, "rules": rules}
    expected_cols, expected_parts = expected_schema(spec)
    actual_cols = [(c["Name"], c["Type"]) for c in glue_table["StorageDescriptor"]["Columns"]]
    actual_parts = [(c["Name"], c["Type"]) for c in glue_table.get("PartitionKeys", [])]
    schema_ok = actual_cols == expected_cols and actual_parts == expected_parts
    rules["R1_schema"] = {
        "passed": schema_ok,
        "detail": "matches the output contract" if schema_ok else {
            "missing": [c for c in expected_cols if c not in actual_cols],
            "unexpected": [c for c in actual_cols if c not in expected_cols],
            "order_matches": [n for n, _ in actual_cols] == [n for n, _ in expected_cols],
            "partitions": actual_parts,
        },
    }

    key, raw_key = spec["primary_key"], spec.get("raw_key", spec["primary_key"])
    tokens = ", ".join(literal(t) for t in NULL_TOKENS)
    raw_not_null = " AND ".join(f"trim(lower(CAST({ident(c)} AS varchar))) NOT IN ({tokens})" for c in raw_key)
    key_null = " OR ".join(f"{ident(c)} IS NULL" for c in key)
    custom = [(r, *_rule_sql(r, table)) for r in spec.get("rules", [])]
    select = [f"count(*) AS total_rows",
              f"count(*) - count(DISTINCT {_key_sql(key)}) AS dup_keys",
              f"count_if({key_null}) AS null_keys"]
    select += [f"{count_sql} AS r{i}" for i, (_, count_sql, _, _) in enumerate(custom)]
    select += [f"{_info_sql(item)} AS i{i}" for i, item in enumerate(spec.get("info", []))]
    try:
        raw = run_query(f"SELECT count(DISTINCT {_key_sql(raw_key, raw=True)}) AS n "
                        f"FROM {where['raw_table']} WHERE {raw_not_null}", RAW_DB)[0]
        m = run_query(f"SELECT {', '.join(select)} FROM {table}", CLEAN_DB)[0]
    except AthenaQueryError as e:
        rules["R2_onwards"] = {"passed": False, "detail": f"validation query failed: {e}"}
        return {"dataset": dataset, "passed": False, "rules": rules}

    n = {k: int(v) if v is not None else 0 for k, v in m.items()}
    raw_keys, key_names = int(raw["n"]), ", ".join(key)
    rules["R2_no_rows_lost"] = {"passed": n["total_rows"] == raw_keys,
                                "detail": f"{n['total_rows']} clean rows vs {raw_keys} distinct raw keys ({key_names})"}
    rules["R3_unique_key"] = {"passed": n["dup_keys"] == 0 and n["null_keys"] == 0,
                              "detail": f"{n['dup_keys']} duplicate and {n['null_keys']} null keys ({key_names})"}
    for i, (rule, _, _, detail) in enumerate(custom):
        rules[rule["id"]] = {"passed": n[f"r{i}"] == 0, "detail": f"{n[f'r{i}']} {detail}"}

    # A count alone cannot be fixed: show the script writer which values broke each rule.
    # Only queried for failed rules; "hashed" rules never get examples (values may be raw PII).
    example_sql = {
        "R2_no_rows_lost": (
            f"SELECT DISTINCT {_key_sql(raw_key, raw=True)} AS missing_key FROM {RAW_DB}.{where['raw_table']} "
            f"WHERE {raw_not_null} AND {_key_sql(raw_key, raw=True)} NOT IN "
            f"(SELECT {_key_sql(key)} FROM {CLEAN_DB}.{table} WHERE NOT ({key_null}))"),
        "R3_unique_key": (
            f"SELECT {_key_sql(key)} AS key, count(*) AS copies FROM {table} "
            f"GROUP BY 1 HAVING count(*) > 1 OR {_key_sql(key)} IS NULL"),
    }
    for rule, _, examples, _ in custom:
        if examples:
            example_sql[rule["id"]] = examples
    for name, sql in example_sql.items():
        if not rules[name]["passed"]:
            try:
                rules[name]["examples"] = run_query(f"{sql} LIMIT {MAX_EXAMPLES}", CLEAN_DB)
            except AthenaQueryError as e:
                rules[name]["examples"] = f"could not fetch examples: {e}"

    return {
        "dataset": dataset,
        "passed": all(r["passed"] for r in rules.values()),
        "rules": rules,
        "info": {item["name"]: n[f"i{i}"] for i, item in enumerate(spec.get("info", []))},
    }


def make_validation_tool(dataset: str):
    """run_athena_validation bound to one dataset, for the Execution Agent running its draft."""

    @tool
    def run_athena_validation() -> str:
        """Check the cleaned table of this run's dataset against its approved spec's rules.

        Always checked: R1 schema matches the contract; R2 no rows lost vs the raw table;
        R3 primary key unique and not null. Then every rule in the dataset's spec.
        Returns JSON with overall "passed", each rule's result and detail, up to 5 example
        offending values for failed rules (never for PII rules), and info counts.
        """
        progress.info("run_athena_validation: checking %s.%s", CLEAN_DB, dataset)
        result = validate_output(dataset)
        failed = [name for name, r in result["rules"].items() if not r["passed"]]
        progress.info("run_athena_validation: %s", "PASSED" if result["passed"] else "FAILED " + ", ".join(failed))
        return json.dumps(result)

    return run_athena_validation
