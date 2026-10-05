"""A CSV copy of a cleaned dataset, for people who want a file rather than a table.

The table stays Parquet (typed; what validation and the docs read). After a passing run
the clean table is read back with Athena - which writes every result as a UTF-8 CSV with
a header row - and that file is copied to s3://etl-copilot-ak/reports/<dataset>/<dataset>.csv.
Columns come out in the contract's order, partition last. Runs as the Execution role
(it can read athena-results/ and write reports/; no new permission).
"""
from aws_session import execution_session
from athena import run_to_completion
from config import BUCKET, CLEAN_DB, REPORTS_PREFIX
from tools.specs import ident, load_spec, paths


def csv_key(dataset: str) -> str:
    return f"{REPORTS_PREFIX}{paths(dataset)['clean_table']}/{dataset}.csv"


def export_csv(dataset: str) -> str:
    """Write the clean table as CSV next to its docs and return its s3:// URI."""
    spec = load_spec(dataset)
    columns = [c["name"] for c in spec["columns"]] + ([spec["partition"]["name"]] if spec.get("partition") else [])
    sql = (f"SELECT {', '.join(ident(c) for c in columns)} FROM {paths(dataset)['clean_table']} "
           f"ORDER BY {', '.join(ident(k) for k in spec['primary_key'])}")
    query_id = run_to_completion(sql, CLEAN_DB, timeout_s=300)
    session = execution_session()
    source = session.client("athena").get_query_execution(QueryExecutionId=query_id)[
        "QueryExecution"]["ResultConfiguration"]["OutputLocation"]  # s3://etl-copilot-ak/athena-results/<id>.csv
    bucket, key = source[len("s3://"):].split("/", 1)
    session.client("s3").copy_object(Bucket=BUCKET, Key=csv_key(dataset), CopySource={"Bucket": bucket, "Key": key},
                                     ContentType="text/csv; charset=utf-8", MetadataDirective="REPLACE")
    return f"s3://{BUCKET}/{csv_key(dataset)}"
