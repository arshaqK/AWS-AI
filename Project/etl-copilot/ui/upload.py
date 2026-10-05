"""The console's only manual step: a CSV into s3://etl-copilot-ak/landing/<dataset>/.

Every check runs before anything reaches S3. The upload uses your own AWS login - the
Execution role the agents' tools run as cannot write to landing/.
"""
import csv
import io
import re
from pathlib import Path
from typing import List

BUCKET = "etl-copilot-ak"
LANDING_ROOT = "landing/"
MAX_BYTES = 50 * 1024 * 1024
DATASET_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")  # the pipeline's rule: tools/specs.py DATASET_RE


class UploadError(ValueError):
    """The file was refused; the message says why, in plain words."""


def dataset_from_filename(name: str) -> str:
    """support_tickets.csv -> support_tickets; 'Q3 Orders-Export.CSV' -> q3_orders_export."""
    stem = re.sub(r"[^a-z0-9]+", "_", Path(name).stem.lower()).strip("_")
    if stem and not stem[0].isalpha():
        stem = "d_" + stem
    return stem[:40]


def check_dataset(dataset: str) -> str:
    if not DATASET_RE.match(dataset or ""):
        raise UploadError("The dataset name must be 2-40 characters: lower-case letters, digits and _, "
                          "starting with a letter.")
    return dataset


def check_csv(name: str, data: bytes) -> dict:
    """Refuse anything that is not a comma-separated text file with a proper header."""
    if not name.lower().endswith(".csv"):
        raise UploadError(f"{name} is not a .csv file. ETL Copilot only accepts CSV.")
    if not data:
        raise UploadError(f"{name} is empty.")
    if len(data) > MAX_BYTES:
        raise UploadError(f"{name} is {len(data) / 1e6:.0f} MB; the limit is {MAX_BYTES // 1_000_000} MB.")
    if b"\x00" in data[:65536]:
        raise UploadError(f"{name} is a binary file, not a text CSV (was it renamed from .xlsx?).")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UploadError(f"{name} is not UTF-8 text. Export it as 'CSV UTF-8'.")
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        raise UploadError(f"{name} has no rows.")
    header = [h.strip() for h in rows[0]]
    if len(header) == 1 and any(sep in header[0] for sep in (";", "\t", "|")):
        raise UploadError(f"{name} is not comma-separated (it looks like ';', tab or '|' separated).")
    if any(not h for h in header):
        raise UploadError(f"{name} has an empty column name in its header.")
    duplicates = sorted({h for h in header if header.count(h) > 1})
    if duplicates:
        raise UploadError(f"{name} repeats column names: {', '.join(duplicates)}.")
    if len(rows) < 2:
        raise UploadError(f"{name} has a header but no data rows.")
    return {"rows": len(rows) - 1, "columns": header}


def existing_files(s3, dataset: str) -> List[str]:
    """File names already in landing/<dataset>/ (they are all read as one table)."""
    prefix = f"{LANDING_ROOT}{check_dataset(dataset)}/"
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix)
    return [o["Key"][len(prefix):] for p in pages for o in p.get("Contents", []) if not o["Key"].endswith("/")]


def upload(s3, dataset: str, name: str, data: bytes) -> str:
    check_csv(name, data)
    key = f"{LANDING_ROOT}{check_dataset(dataset)}/{Path(name).name}"
    s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType="text/csv")
    return f"s3://{BUCKET}/{key}"
