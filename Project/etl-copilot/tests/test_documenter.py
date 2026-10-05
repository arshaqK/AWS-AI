"""Step 4.3 gate: data dictionary + quality report for every dataset, checked independently.

For each dataset this writes both documents with the real Documenter (one model call each,
about a minute), reads them back from S3 and checks:
  - every figure in them equals a fresh Athena query written here, separately from the
    code that produced them (nulls and distinct counts per column, row counts),
  - every column has a derivation, and the quality report says PASSED,
  - no personal data: no email or phone pattern, and none of the real raw emails or
    customer names from the CSVs appears anywhere.

Usage (from app/EtlCopilot/):   uv run python ../../tests/test_documenter.py [dataset ...]
"""
import csv
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(os.getenv("ETL_COPILOT_APP", ROOT / "app" / "EtlCopilot"))
sys.path.insert(0, str(APP_DIR))

from agents.documenter import write_docs  # noqa: E402
from athena import run_query  # noqa: E402
from aws_session import execution_session  # noqa: E402
from config import BUCKET  # noqa: E402

DATASETS = sys.argv[1:] or ["orders", "inventory_snapshot", "support_tickets"]
CSV = {"orders": "orders_export.csv", "inventory_snapshot": "inventory_snapshot.csv",
       "support_tickets": "support_tickets.csv"}
results = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def read_s3(uri: str) -> str:
    bucket, key = uri[len("s3://"):].split("/", 1)
    return execution_session().client("s3").get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")


def table_rows(markdown: str, header_start: str):
    """Rows of the markdown table whose header line starts with header_start."""
    lines = markdown.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(header_start))
    rows = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]])
    return rows


def raw_secrets(dataset: str) -> set:
    """Real personal values from the raw CSV that must never appear in the docs."""
    rows = list(csv.DictReader(open(ROOT / "data" / CSV[dataset], encoding="utf-8")))
    found = set()
    for r in rows:
        if r.get("customer_email", "").strip().endswith(".com"):
            found.add(r["customer_email"].strip().lower())
        if r.get("customer_name"):
            found.add(r["customer_name"])
    return found


for ds in DATASETS:
    print(f"=== {ds}")
    out = write_docs(ds)
    dictionary, quality = read_s3(out["data_dictionary"]), read_s3(out["quality_report"])
    check("both documents saved", bool(dictionary) and bool(quality), out["data_dictionary"])

    # independent figures: one query per column, written here
    cols = execution_session().client("glue").get_table(DatabaseName="etl_copilot_clean", Name=ds)["Table"]
    names = [c["Name"] for c in cols["StorageDescriptor"]["Columns"]] + [c["Name"] for c in cols.get("PartitionKeys", [])]
    truth = {}
    for n in names:
        r = run_query(f'SELECT count(*) AS t, count(*) - count("{n}") AS nulls, count(DISTINCT "{n}") AS d FROM "{ds}"',
                      "etl_copilot_clean")[0]
        truth[n] = (int(r["nulls"]), int(r["d"]))
        total = int(r["t"])
    documented = {re.match(r"`([^`]+)`", row[0]).group(1): row for row in table_rows(dictionary, "| Column |")}
    check("dictionary lists every column", set(documented) == set(names),
          f"missing {sorted(set(names) - set(documented))}" if set(documented) != set(names) else f"{len(names)} columns")
    wrong = [n for n in names if n in documented and (int(documented[n][2]), int(documented[n][4])) != truth[n]]
    check("nulls and distinct counts equal fresh Athena counts", not wrong,
          f"differ: {[(n, documented[n][2], documented[n][4], truth[n]) for n in wrong][:3]}")
    stated_rows = int(re.search(r"\| Rows \| (\d+) \|", dictionary).group(1))
    check("dictionary row count", stated_rows == total, f"{stated_rows} vs {total}")

    facts = dict(row[:2] for row in table_rows(quality, "| | |"))
    raw_rows = int(run_query(f'SELECT count(*) AS n FROM "raw_{ds}"', "etl_copilot_raw")[0]["n"])
    check("quality report raw rows", int(facts["Raw rows (CSV, including re-sent duplicates)"]) == raw_rows,
          f"{facts['Raw rows (CSV, including re-sent duplicates)']} vs {raw_rows}")
    check("quality report clean rows", int(facts["Clean rows"]) == total)
    check("quality report says PASSED", "## Result: PASSED" in quality)
    rules = table_rows(quality, "| Rule |")
    check("every rule listed as passed", rules and all(r[1] == "passed" for r in rules), f"{len(rules)} rules")

    derived = re.findall(r"\*\*Derived:\*\* (.+)", dictionary)
    check("every column has a derivation", len(derived) == len(names) and "not described" not in derived,
          f"{sum(d != 'not described' for d in derived)}/{len(names)}")
    # the model must not restate statistics: no large figure from this table (row counts,
    # null or distinct counts >= 100) may appear in its prose. Small numbers are skipped:
    # they collide by chance with formats like decimal(10,2) or "13-digit epoch ms".
    stats = {v for v in {total, raw_rows} | {v for nd in truth.values() for v in nd} if v >= 100}
    caveats = dictionary.split("## Caveats", 1)[1] if "## Caveats" in dictionary else ""
    prose = " ".join(derived) + " " + caveats
    restated = sorted({int(n) for n in re.findall(r"(?<![\d.])\d+(?![\d.])", prose)} & stats)
    check("prose restates no statistics", not restated, f"figures found in prose: {restated}")

    text = dictionary + quality
    patterns = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text) + re.findall(r"\+\d[\d ()-]{9,}\d|\b\d{3}\.\d{3}\.\d{4}\b", text)
    patterns = [p for p in patterns if p.rstrip(".") not in ("name@domain.tld",)]
    leaked = [s for s in raw_secrets(ds) if s in text]
    check("no email/phone patterns", not patterns, str(patterns[:2]))
    check("none of the raw emails or customer names appear", not leaked, str(leaked[:2]))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
