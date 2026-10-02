"""Step 3.2 gate: ask the Script Writer for a draft, then dry-run it locally.

Costs one Script Writer run (Sonnet on Bedrock, a few cents) plus a few Athena queries.
Starts no Glue job: the draft is only saved to s3://etl-copilot-ak/scripts/drafts/.

Usage (from app/EtlCopilot/):
    uv run --with "pandas<3" python ../../tests/test_script_writer.py
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dry_run_draft import dry_run  # noqa: E402  (also puts app/EtlCopilot on sys.path)

from agents.script_writer import script_writer  # noqa: E402
from tools.drafts import read_draft  # noqa: E402

# Both vendor conventions are given here. Step 3.4 leaves the 1900-01-01 one out on purpose.
TASK = ("Write the ETL for the Trailhead Outfitters orders export in landing/orders/. "
        "Vendor conventions: slash dates are DD/MM/YYYY; 1900-01-01 is the vendor's "
        "placeholder for an unknown date.")

started = time.time()
reply = script_writer("orders", TASK)
print(reply)
print(f"\n[script writer took {time.time() - started:.0f}s]\n")

match = re.search(r"DRAFT_ID:\W*([0-9a-f]{12})", reply)  # tolerates **bold** and `code` markup
if not match:
    sys.exit("FAIL  no DRAFT_ID in the Script Writer's reply")
draft_id = match.group(1)
sys.exit(0 if dry_run(read_draft(draft_id), f"draft {draft_id}") else 1)
