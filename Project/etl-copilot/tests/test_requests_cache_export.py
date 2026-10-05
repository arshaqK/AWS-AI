"""Fixes after the 5.3 test: exact change requests, one profile per data version, CSV export.

  A. A REJECT with a reason reaches the writer word for word, once, and only for its own
     dataset - however the Supervisor briefs the writer. Revisions list 'What changed'.
  B. The raw table is profiled once per version of landing/<dataset>/: a revision reuses
     the profile (and does not narrate a scan); a changed file is profiled again.
  C. After a passing run the clean table is also written as CSV: header in the contract's
     order, one row per clean row, next to the docs.

Uses AWS (Athena, S3 reads; rewrites reports/orders/orders.csv). No model calls.
Usage (from app/EtlCopilot/):  uv run python ../../tests/test_requests_cache_export.py
"""
import asyncio
import csv
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(os.getenv("ETL_COPILOT_APP", ROOT / "app" / "EtlCopilot"))))

import approvals  # noqa: E402
import narration  # noqa: E402
from agents import script_writer as sw  # noqa: E402
from athena import run_query  # noqa: E402
from aws_session import execution_session  # noqa: E402
from config import BUCKET, CLEAN_DB  # noqa: E402
from tools import profile as prof  # noqa: E402
from tools.export import csv_key, export_csv  # noqa: E402
from tools.specs import load_spec  # noqa: E402

results = []
DRAFT = "79a94743fde0"  # an orders draft in S3
REQUEST = "keep customer_name as is, parse dates as DD/MM, drop the notes column, uppercase currency"


def check(name, passed, detail=""):
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


print("--- A. the engineer's exact words reach the writer ---")
msg = approvals.handle_human_message(f"REJECT {DRAFT} {REQUEST}")
check("the Supervisor is told the writer gets the exact words", "exact words automatically" in msg)
check("another dataset's writer gets nothing", approvals.take_request("draft", "support_tickets") == "")
block = approvals.take_request("draft", "orders")
check("the whole request, every comma-separated part, word for word", f'"{REQUEST}"' in block and "EVERY part" in block)
check("used once", approvals.take_request("draft", "orders") == "")
approvals.handle_human_message(f"REJECT {DRAFT}")
check("a reject without a reason stores nothing", approvals.take_request("draft", "orders") == "")
approvals.handle_human_message(f"REJECT 000000000000 {REQUEST}")
check("an unknown draft id stores nothing", approvals._requests == {})

captured = {}


class FakeAgent:
    def __init__(self, **kwargs):
        pass

    def __call__(self, task):
        captured["task"] = task
        return "no draft"


real_agent, sw.Agent = sw.Agent, FakeAgent
approvals.handle_human_message(f"REJECT {DRAFT} {REQUEST}")
sw.script_writer("orders", "Revise the draft: keep customer_name.")  # a Supervisor that shortened it
sw.Agent = real_agent
check("even when the Supervisor's task is shortened, the writer sees the full request",
      "keep customer_name." in captured["task"] and REQUEST in captured["task"])

gates = []
real_post, narration.post = narration.post, gates.append
from agents import spec_writer as spw  # noqa: E402
spw.read_spec_draft = lambda sid: load_spec("orders")
spw._narrate_gate(f"SPEC_ID: abcdefabcdef\nNEEDS CONFIRMATION:\n- nothing\nChanged since 111111111111:\n"
                  "- `customer_name` kept\n- slash dates are DD/MM", saved=["abcdefabcdef"])
narration.post = real_post
check("a revised contract card lists what changed", gates and gates[0]["changed"] == ["`customer_name` kept",
                                                                                     "slash dates are DD/MM"], str(gates[:1]))

print("--- B. one profile per version of the data ---")
calls = []
real_build = prof.build_profile
prof.build_profile = lambda d: calls.append(d) or real_build(d)
prof._profiles.clear()
first, fresh1 = prof.cached_profile("orders")
second, fresh2 = prof.cached_profile("orders")
check("first call scans, the second reuses it", (fresh1, fresh2, calls) == (True, False, ["orders"]))
check("the reused profile is the same profile", first is second)
real_version, prof.landing_version = prof.landing_version, lambda d: (("landing/orders/new.csv", '"etag2"'),)
_, fresh3 = prof.cached_profile("orders")
prof.landing_version = real_version
check("a changed file is scanned again", fresh3 is True and calls == ["orders", "orders"])


async def narrated(times):
    got = []
    channel = narration.open_channel(asyncio.get_running_loop(), got.append)
    for _ in range(times):
        await asyncio.to_thread(prof.get_dataset_profile, "orders")
    await asyncio.sleep(0.05)
    narration.close_channel(channel)
    return got


prof.build_profile = real_build
prof._profiles.clear()
asyncio.run(narrated(1))  # warm the cache, as an earlier request would
check("a later request that reuses the profile narrates no scan", asyncio.run(narrated(2)) == [])
prof._profiles.clear()
scanned = [g["narration"]["type"] for g in asyncio.run(narrated(2))]
check("a request that really scans narrates it once, with a status line while it runs",
      scanned.count("step") == 1 and scanned.count("activity") == 1, str(scanned))

print("--- C. the clean table as CSV ---")
uri = export_csv("orders")
check("written next to the docs", uri == f"s3://{BUCKET}/reports/orders/orders.csv" and csv_key("orders") == "reports/orders/orders.csv")
body = execution_session().client("s3").get_object(Bucket=BUCKET, Key=csv_key("orders"))["Body"].read().decode("utf-8")
rows = list(csv.reader(io.StringIO(body)))
spec = load_spec("orders")
check("header is the contract's columns, partition last",
      rows[0] == [c["name"] for c in spec["columns"]] + [spec["partition"]["name"]], str(rows[0][:4]))
count = int(run_query("SELECT count(*) AS n FROM orders", CLEAN_DB)[0]["n"])
check("one CSV row per clean row", len(rows) - 1 == count, f"{len(rows) - 1} vs {count}")
docs = narration.docs_step({"data_dictionary": "s3://x/dd.md", "quality_report": "s3://x/qr.md", "clean_csv": uri})
check("the docs step offers the CSV", docs["files"]["clean_csv"] == uri and any("CSV" in b for b in docs["bullets"]))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
