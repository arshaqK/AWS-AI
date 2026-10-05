"""Run an Athena query in the etl-copilot-wg workgroup and return the rows."""
import time
from typing import Dict, List, Optional

from aws_session import execution_session
from config import WORKGROUP

POLL_INTERVAL_S = 1


class AthenaQueryError(RuntimeError):
    """The query failed, was cancelled, or timed out. The message says which, and why."""


def run_query(sql: str, database: str, timeout_s: int = 90) -> List[Dict[str, Optional[str]]]:
    """Run `sql` against `database` and return one dict per row (column -> string value).

    All values come back as strings (Athena's wire format); SQL NULL becomes None.
    """
    athena = execution_session().client("athena")
    query_id = run_to_completion(sql, database, timeout_s)

    header: Optional[List[str]] = None
    rows: List[Dict[str, Optional[str]]] = []
    for page in athena.get_paginator("get_query_results").paginate(QueryExecutionId=query_id):
        for row in page["ResultSet"]["Rows"]:
            values = [cell.get("VarCharValue") for cell in row["Data"]]
            if header is None:  # the first row of a SELECT result is the column names
                header = values
                continue
            rows.append(dict(zip(header, values)))
    return rows


def run_to_completion(sql: str, database: str, timeout_s: int = 90) -> str:
    """Start `sql` in the workgroup, wait for it, and return the query id (its result CSV
    sits at the query's OutputLocation)."""
    athena = execution_session().client("athena")
    query_id = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": database},
        WorkGroup=WORKGROUP,  # the workgroup sets and enforces the result location
    )["QueryExecutionId"]

    deadline = time.time() + timeout_s
    while True:
        status = athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason", "no reason given")
            raise AthenaQueryError(f"{state}: {reason} (query {query_id})")
        if time.time() > deadline:
            athena.stop_query_execution(QueryExecutionId=query_id)
            raise AthenaQueryError(f"timed out after {timeout_s}s (query {query_id})")
        time.sleep(POLL_INTERVAL_S)
    return query_id
