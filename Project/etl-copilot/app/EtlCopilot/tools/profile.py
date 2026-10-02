"""Profile a dataset's raw landing table so the agents know what they are cleaning.

Glue Data Quality only counts real NULLs, so it misses "N/A" / "null" / "-" and says
nothing about formats. This profile is built from Athena queries instead and is
generic: columns come from the Glue catalog, nothing about any dataset is hard-coded.
It needs no spec, so it can profile a new dataset before its spec is proposed.
"""
import json
from typing import Dict, List

from strands import tool

from athena import run_query
from aws_session import execution_session
from config import RAW_DB
from tools.specs import paths, raw_table_exists

NULL_TOKENS = ("", "n/a", "null", "-")  # compared after trim + lower
SAMPLE_ROWS = 10
TOP_SHAPES = 5
LOW_CARDINALITY = 20  # list every distinct value for columns with at most this many
MAX_SHAPE_LEN = 60


def _ident(column: str) -> str:
    """Quote a column name for Athena."""
    return '"' + column.replace('"', '""') + '"'


def _text(column: str) -> str:
    """The value as text. The crawler types some columns (e.g. "2.0" quantities -> double),
    so every string function below works on the cast, whatever the catalog type is."""
    return f"CAST({_ident(column)} AS varchar)"


def _is_null_token(column: str) -> str:
    tokens = ", ".join("'" + t + "'" for t in NULL_TOKENS)
    return f"({_ident(column)} IS NULL OR trim(lower({_text(column)})) IN ({tokens}))"


def _shape(column: str) -> str:
    """Digits -> 9 (kept one per digit, so date layouts stay visible), runs of letters -> a."""
    return (f"regexp_replace(regexp_replace({_text(column)}, '[0-9]', '9'), "
            f"'\\p{{L}}+', 'a')")


def _columns(raw_table: str) -> List[Dict[str, str]]:
    table = execution_session().client("glue").get_table(DatabaseName=RAW_DB, Name=raw_table)["Table"]
    return [{"name": c["Name"], "type": c["Type"]} for c in table["StorageDescriptor"]["Columns"]]


def build_profile(dataset: str) -> dict:
    """Plain function (no LLM) so tests can call it directly."""
    RAW_TABLE = paths(dataset)["raw_table"]  # validates the dataset name
    if not raw_table_exists(dataset):
        raise LookupError(f"no raw table {RAW_DB}.{RAW_TABLE}: upload the CSV to "
                          f"landing/{dataset}/ and run the crawler first")
    columns = _columns(RAW_TABLE)
    names = [c["name"] for c in columns]

    # 1) row count, null-ish count and distinct count for every column, in one query
    parts = ["count(*) AS row_count"]
    for i, name in enumerate(names):
        parts.append(f"count_if({_is_null_token(name)}) AS n{i}")
        parts.append(f"count(DISTINCT {_ident(name)}) AS d{i}")
    stats = run_query(f"SELECT {', '.join(parts)} FROM {RAW_TABLE}", RAW_DB)[0]

    # 2) which null spellings appear, per column
    token_sql = " UNION ALL ".join(
        f"SELECT '{name}' AS col, {_text(name)} AS token, count(*) AS n FROM {RAW_TABLE} "
        f"WHERE {_is_null_token(name)} GROUP BY 2"
        for name in names
    )
    null_tokens: Dict[str, Dict[str, int]] = {}
    for r in run_query(token_sql, RAW_DB):
        label = "<empty>" if not r["token"] else r["token"]
        null_tokens.setdefault(r["col"], {})[label] = int(r["n"])

    # 3) value shapes per column (format detector), top N each
    shape_sql = " UNION ALL ".join(
        f"SELECT '{name}' AS col, {_shape(name)} AS shape, count(*) AS n FROM {RAW_TABLE} "
        f"WHERE NOT {_is_null_token(name)} GROUP BY 2"
        for name in names
    )
    shapes: Dict[str, List[dict]] = {}
    for r in sorted(run_query(shape_sql, RAW_DB), key=lambda r: -int(r["n"])):
        bucket = shapes.setdefault(r["col"], [])
        if len(bucket) < TOP_SHAPES:
            bucket.append({"shape": (r["shape"] or "")[:MAX_SHAPE_LEN], "n": int(r["n"])})

    # 4) every distinct value of low-cardinality columns (reveals spelling variants)
    low_card = [n for i, n in enumerate(names) if 0 < int(stats[f"d{i}"]) <= LOW_CARDINALITY]
    values: Dict[str, Dict[str, int]] = {}
    if low_card:
        value_sql = " UNION ALL ".join(
            f"SELECT '{name}' AS col, {_text(name)} AS value, count(*) AS n FROM {RAW_TABLE} GROUP BY 2"
            for name in low_card
        )
        for r in sorted(run_query(value_sql, RAW_DB), key=lambda r: -int(r["n"])):
            values.setdefault(r["col"], {})[r["value"] if r["value"] else "<empty>"] = int(r["n"])

    sample = run_query(f"SELECT * FROM {RAW_TABLE} LIMIT {SAMPLE_ROWS}", RAW_DB)

    return {
        "dataset": dataset,
        "table": f"{RAW_DB}.{RAW_TABLE}",
        "row_count": int(stats["row_count"]),
        "columns": [
            {
                "name": c["name"],
                "catalog_type": c["type"],
                "null_like": int(stats[f"n{i}"]),
                "null_tokens": null_tokens.get(c["name"], {}),
                "distinct": int(stats[f"d{i}"]),
                "top_shapes": shapes.get(c["name"], []),
                "values": values.get(c["name"]),  # only for low-cardinality columns
            }
            for i, c in enumerate(columns)
        ],
        "sample_rows": sample,
        "legend": "shape: each digit -> 9, each run of letters -> a. null_like counts NULL, "
                  "'', 'N/A', 'null' and '-' (case/whitespace-insensitive). Columns whose "
                  "catalog_type is not string were already parsed by Athena, so their shapes "
                  "show the parsed value; the ETL still reads the raw file as text.",
    }


@tool
def get_dataset_profile(dataset: str) -> str:
    """Profile a dataset's raw landing table (etl_copilot_raw.raw_<dataset>).

    Returns JSON with, per column: the catalog type, how many values are null-like and
    which null spellings appear, the distinct count, the most common value shapes (date,
    phone and price formats show up here), and every distinct value for low-cardinality
    columns (spelling variants show up here). Also returns the row count and sample rows.

    Args:
        dataset: the dataset name, i.e. its folder under landing/ (e.g. "orders").
    """
    try:
        return json.dumps(build_profile(dataset), ensure_ascii=False)
    except (LookupError, ValueError) as e:  # unknown dataset: tell the model, don't crash
        return f"ERROR: {e}"
