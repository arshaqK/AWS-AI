"""Dataset specs: everything that is specific to one dataset, approved by the engineer.

A spec says what the clean table must look like (columns, types, partition, key), how
PII is handled, the dataset's cleaning rules and its validation rules.

- Approved specs: s3://etl-copilot-ak/scripts/contracts/<dataset>.json - the contract the
  Script Writer drafts against and the validation rules check.
- Proposed specs: scripts/contracts/drafts/<spec_id>.json, where spec_id is a hash of the
  content. Only the engineer's "APPROVE SPEC <spec_id>" (read by the entrypoint, never by
  a model) turns a proposal into the dataset's contract - see approvals.py.

Folders and table names are DERIVED from the dataset name, never stored in the spec, so
a spec - even one proposed by a model - cannot point anywhere else. Every value that ends
up in SQL is checked here first (names by pattern, types from a fixed list, literals
escaped), so a spec cannot inject SQL into the validation queries.
"""
import datetime
import hashlib
import json
import re
import sys
from typing import List, Optional

from botocore.exceptions import ClientError

from aws_session import execution_session
from config import (BUCKET, CLEAN_DB, CONTRACTS_PREFIX, LANDING_ROOT, RAW_DB, SPEC_DRAFTS_PREFIX,
                    STAGING_ROOT)

DATASET_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RULE_ID_RE = re.compile(r"^R\d{1,2}_[a-z0-9_]{1,40}$")
SPEC_ID_RE = re.compile(r"^[0-9a-f]{12}$")
TYPE_RE = re.compile(r"^(string|int|bigint|double|boolean|date|timestamp|decimal\((\d{1,2}),(\d{1,2})\))$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CORE_RULES = ("R1_schema", "R2_no_rows_lost", "R3_unique_key")  # always checked, from the contract
RULE_TYPES = {"date_range", "allowed_values", "matches", "hashed"}
INFO_TYPES = {"null_count", "distinct_count", "false_count"}
PII_TREATMENTS = {"hash", "drop", "redact"}
MAX_SPEC_BYTES = 50_000


class SpecNotFound(LookupError):
    """The dataset has no approved spec yet."""


def paths(dataset: str) -> dict:
    """Every location of a dataset, derived from its name alone."""
    if not DATASET_RE.match(dataset or ""):
        raise ValueError(f"not a dataset name: {dataset!r} (lower-case letters, digits, _)")
    return {
        "landing_prefix": f"{LANDING_ROOT}{dataset}/",
        "staging_prefix": f"{STAGING_ROOT}{dataset}/",
        "landing_uri": f"s3://{BUCKET}/{LANDING_ROOT}{dataset}/",
        "staging_uri": f"s3://{BUCKET}/{STAGING_ROOT}{dataset}/",
        "raw_db": RAW_DB,
        "raw_table": f"raw_{dataset}",
        "clean_db": CLEAN_DB,
        "clean_table": dataset,
    }


# ---------- SQL helpers: everything a spec puts into SQL goes through these ----------

def ident(name: str) -> str:
    """A column name as an Athena identifier (names are pattern-checked by check_spec)."""
    return '"' + name.replace('"', '""') + '"'


def literal(value: str) -> str:
    """A value as an Athena string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def date_sql(value: str) -> str:
    return "current_date" if value == "today" else f"DATE {literal(value)}"


# ---------- validation of a spec ----------

def _is_str_list(value, max_items: int, max_len: int) -> bool:
    return (isinstance(value, list) and 0 < len(value) <= max_items
            and all(isinstance(v, str) and 0 < len(v) <= max_len for v in value))


def check_spec(spec: dict) -> List[str]:
    """Return every problem with a spec (empty list = valid)."""
    if not isinstance(spec, dict):
        return ["a spec must be a JSON object"]
    if len(json.dumps(spec)) > MAX_SPEC_BYTES:
        return [f"spec is larger than {MAX_SPEC_BYTES} bytes"]
    p = []
    allowed_keys = {"dataset", "description", "primary_key", "raw_key", "columns", "partition",
                    "pii", "cleaning_rules", "rules", "info"}
    p += [f"unknown top-level key {k!r}" for k in spec if k not in allowed_keys]

    dataset = spec.get("dataset")
    if not isinstance(dataset, str) or not DATASET_RE.match(dataset):
        p.append("dataset: lower-case letters, digits and _, 2-40 chars, starting with a letter")
    if not isinstance(spec.get("description", ""), str) or len(spec.get("description", "")) > 500:
        p.append("description: text, at most 500 chars")

    columns = spec.get("columns")
    types = {}
    if not isinstance(columns, list) or not 0 < len(columns) <= 200:
        p.append("columns: a list of 1-200 {name, type, description}")
        columns = []
    for i, col in enumerate(columns):
        if not isinstance(col, dict):
            p.append(f"columns[{i}]: must be an object")
            continue
        name, typ = col.get("name"), col.get("type")
        if not isinstance(name, str) or not NAME_RE.match(name):
            p.append(f"columns[{i}].name {name!r}: lower-case letters, digits and _")
            continue
        if name in types:
            p.append(f"column {name!r} appears twice")
        if not isinstance(typ, str) or not TYPE_RE.match(typ):
            p.append(f"column {name!r}: type {typ!r} not one of string, int, bigint, double, "
                     "boolean, date, timestamp, decimal(p,s)")
        if not isinstance(col.get("description", ""), str) or len(col.get("description", "")) > 300:
            p.append(f"column {name!r}: description is text, at most 300 chars")
        types[name] = typ

    part = spec.get("partition")
    if part is not None:
        if not isinstance(part, dict) or not NAME_RE.match(str(part.get("name", ""))):
            p.append("partition: null, or {name, type: string, rule}")
        else:
            if part["name"] in types:
                p.append(f"partition {part['name']!r} must not also be a column")
            if part.get("type") != "string":
                p.append("partition type must be string")
            if not isinstance(part.get("rule"), str) or not 0 < len(part["rule"]) <= 300:
                p.append("partition.rule: how the value is derived, at most 300 chars")

    key = spec.get("primary_key")
    if not isinstance(key, list) or not 0 < len(key) <= 4 or not all(k in types for k in key or []):
        p.append("primary_key: 1-4 names of output columns")
    raw_key = spec.get("raw_key", key)
    if not isinstance(raw_key, list) or not isinstance(key, list) or len(raw_key) != len(key) \
            or not all(isinstance(k, str) and NAME_RE.match(k) for k in raw_key):
        p.append("raw_key: the raw column behind each primary_key column (same length)")

    pii = spec.get("pii", [])
    if not isinstance(pii, list):
        p.append("pii: a list of {column, treatment, output, note}")
        pii = []
    for i, item in enumerate(pii):
        if not isinstance(item, dict) or not NAME_RE.match(str(item.get("column", ""))):
            p.append(f"pii[{i}]: needs the raw column name")
            continue
        treatment, output = item.get("treatment"), item.get("output")
        if treatment not in PII_TREATMENTS:
            p.append(f"pii[{i}].treatment must be one of {sorted(PII_TREATMENTS)}")
        if treatment == "drop" and output is not None:
            p.append(f"pii[{i}]: a dropped column has output null")
        if treatment in ("hash", "redact") and types.get(output) != "string":
            p.append(f"pii[{i}].output must be a string column of the contract")

    if not _is_str_list(spec.get("cleaning_rules"), 60, 400):
        p.append("cleaning_rules: a list of 1-60 instructions, each at most 400 chars")

    rule_ids = set(CORE_RULES)
    for i, rule in enumerate(spec.get("rules", []) if isinstance(spec.get("rules", []), list) else [None]):
        if not isinstance(rule, dict):
            p.append(f"rules[{i}]: must be an object")
            continue
        rid, rtype = rule.get("id"), rule.get("type")
        where = f"rule {rid!r}"
        if not isinstance(rid, str) or not RULE_ID_RE.match(rid):
            p.append(f"rules[{i}].id: like 'R4_valid_dates'")
        elif rid in rule_ids:
            p.append(f"{where}: id used twice (R1-R3 are built in)")
        rule_ids.add(rid)
        if rtype not in RULE_TYPES:
            p.append(f"{where}: type must be one of {sorted(RULE_TYPES)}")
            continue
        if rtype == "hashed":
            cols = rule.get("columns")
            if not isinstance(cols, list) or not cols or any(types.get(c) != "string" for c in cols):
                p.append(f"{where}: columns must be string columns of the contract")
            continue
        col = rule.get("column")
        if col not in types:
            p.append(f"{where}: column {col!r} is not in the contract")
            continue
        if rtype == "date_range":
            if not (types[col] in ("date", "timestamp")):
                p.append(f"{where}: {col} is not a date/timestamp column")
            for bound in ("min", "max"):
                v = rule.get(bound)
                if not (v == "today" or (isinstance(v, str) and DATE_RE.match(v) and _real_date(v))):
                    p.append(f"{where}: {bound} must be YYYY-MM-DD or 'today'")
        elif rtype == "allowed_values":
            if not _is_str_list(rule.get("values"), 200, 100):
                p.append(f"{where}: values is a list of 1-200 strings")
        elif rtype == "matches":
            pattern = rule.get("pattern")
            try:
                ok = isinstance(pattern, str) and 0 < len(pattern) <= 200 and re.compile(pattern)
            except re.error:
                ok = False
            if not ok:
                p.append(f"{where}: pattern must be a valid regex, at most 200 chars")
        if not isinstance(rule.get("description", ""), str) or len(rule.get("description", "")) > 200:
            p.append(f"{where}: description is text, at most 200 chars")

    info_names = set()
    for i, item in enumerate(spec.get("info", []) if isinstance(spec.get("info", []), list) else [None]):
        if not isinstance(item, dict) or not NAME_RE.match(str(item.get("name", ""))):
            p.append(f"info[{i}]: needs a name (lower-case letters, digits, _)")
            continue
        if item["name"] in info_names:
            p.append(f"info {item['name']!r} appears twice")
        info_names.add(item["name"])
        if item.get("type") not in INFO_TYPES or item.get("column") not in types:
            p.append(f"info {item['name']!r}: type in {sorted(INFO_TYPES)} on a contract column")
        elif item["type"] == "false_count" and types[item["column"]] != "boolean":
            p.append(f"info {item['name']!r}: false_count needs a boolean column")
    return p


def _real_date(value: str) -> bool:
    try:
        datetime.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def spec_id_for(spec: dict) -> str:
    canonical = json.dumps(spec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def expected_schema(spec: dict):
    """(columns, partitions) as (name, type) lists - what the clean table must contain."""
    cols = [(c["name"], c["type"]) for c in spec["columns"]]
    parts = [(spec["partition"]["name"], "string")] if spec.get("partition") else []
    return cols, parts


# ---------- S3: approved specs, proposed specs ----------

def _s3():
    return execution_session().client("s3")


def _get_json(key: str) -> Optional[dict]:
    try:
        body = _s3().get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(body.decode("utf-8"))


def load_spec(dataset: str) -> dict:
    """The dataset's approved spec. Raises SpecNotFound if there is none."""
    paths(dataset)  # validates the name
    spec = _get_json(f"{CONTRACTS_PREFIX}{dataset}.json")
    if spec is None:
        raise SpecNotFound(f"dataset {dataset!r} has no approved spec")
    return spec


def raw_table_exists(dataset: str) -> bool:
    try:
        execution_session().client("glue").get_table(DatabaseName=RAW_DB, Name=paths(dataset)["raw_table"])
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityNotFoundException":
            return False
        raise


def save_spec_draft(spec: dict) -> dict:
    """Check a proposed spec and save it under its content hash. Plain function for tests."""
    problems = check_spec(spec)
    if not problems and not raw_table_exists(spec["dataset"]):
        problems = [f"there is no raw table {RAW_DB}.raw_{spec['dataset']}: upload the CSV to "
                    f"landing/{spec['dataset']}/ and run the crawler first"]
    if problems:
        return {"saved": False, "problems": problems}
    spec_id = spec_id_for(spec)
    body = json.dumps(spec, indent=2, ensure_ascii=False).encode("utf-8")
    _s3().put_object(Bucket=BUCKET, Key=f"{SPEC_DRAFTS_PREFIX}{spec_id}.json", Body=body,
                     ContentType="application/json")
    return {"saved": True, "spec_id": spec_id, "dataset": spec["dataset"]}


def read_spec_draft(spec_id: str) -> dict:
    if not SPEC_ID_RE.match(spec_id or ""):
        raise ValueError(f"not a spec id: {spec_id!r}")
    spec = _get_json(f"{SPEC_DRAFTS_PREFIX}{spec_id}.json")
    if spec is None:
        raise LookupError(f"no proposed spec {spec_id}")
    return spec


def promote_spec(spec_id: str) -> str:
    """Make an approved proposal the dataset's contract. Called only by the entrypoint.

    Re-checks the stored proposal (its hash must still equal spec_id, and it must still be
    valid), then writes it as scripts/contracts/<dataset>.json. Returns the dataset name.
    Raises ValueError/LookupError with a reason the engineer can act on.
    """
    spec = read_spec_draft(spec_id)
    if spec_id_for(spec) != spec_id:
        raise ValueError(f"spec {spec_id} changed after it was proposed; propose it again")
    problems = check_spec(spec)
    if problems:
        raise ValueError(f"spec {spec_id} is no longer valid: {problems}")
    body = json.dumps(spec, indent=2, ensure_ascii=False).encode("utf-8")
    _s3().put_object(Bucket=BUCKET, Key=f"{CONTRACTS_PREFIX}{spec['dataset']}.json", Body=body,
                     ContentType="application/json")
    return spec["dataset"]


def list_datasets() -> List[dict]:
    """Every dataset that has a landing folder or an approved spec, and how far along it is."""
    s3 = _s3()
    folders = s3.list_objects_v2(Bucket=BUCKET, Prefix=LANDING_ROOT, Delimiter="/").get("CommonPrefixes", [])
    landed = {f["Prefix"][len(LANDING_ROOT):-1] for f in folders}
    contracts = s3.list_objects_v2(Bucket=BUCKET, Prefix=CONTRACTS_PREFIX, Delimiter="/").get("Contents", [])
    specced = {o["Key"][len(CONTRACTS_PREFIX):-len(".json")] for o in contracts if o["Key"].endswith(".json")}
    raw_tables = set()
    for page in execution_session().client("glue").get_paginator("get_tables").paginate(DatabaseName=RAW_DB):
        raw_tables |= {t["Name"] for t in page["TableList"]}
    return [{"dataset": d, "landing_folder": d in landed, "raw_table": f"raw_{d}" in raw_tables,
             "approved_spec": d in specced}
            for d in sorted(n for n in landed | specced if DATASET_RE.match(n))]


# ---------- engineer-run CLI: seed a spec written by hand (e.g. orders, from Phase 1) ----------

def seed_spec(path: str) -> str:
    """Install a hand-written spec file as a dataset's contract (the engineer IS the approver)."""
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    problems = check_spec(spec)
    if problems:
        raise ValueError(f"{path} is not a valid spec: {problems}")
    body = json.dumps(spec, indent=2, ensure_ascii=False).encode("utf-8")
    _s3().put_object(Bucket=BUCKET, Key=f"{CONTRACTS_PREFIX}{spec['dataset']}.json", Body=body,
                     ContentType="application/json")
    return spec["dataset"]


if __name__ == "__main__":
    # From app/EtlCopilot/:  uv run python -m tools.specs seed datasets/orders.json
    if len(sys.argv) == 3 and sys.argv[1] == "seed":
        print(f"installed spec for dataset {seed_spec(sys.argv[2])!r}")
    else:
        sys.exit("usage: python -m tools.specs seed <path/to/spec.json>")
