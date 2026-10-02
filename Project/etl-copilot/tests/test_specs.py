"""Steps 4.2.1 + 4.2.2 gate: dataset specs, and approving them.

  A. (offline) Specs are validated: orders and the Spec Writer's example pass, malformed or
     SQL-injecting specs are rejected; scripts must stay inside their own dataset's folders.
  B. The installed orders contract is exactly datasets/orders.json.
  C. No spec, no script: the Script Writer refuses a dataset without an approved spec
     (in code, before any model runs); the Spec Writer refuses a dataset without raw data.
  D. Only an exact "APPROVE SPEC <id>" installs a proposal; a wrong id, a changed proposal
     or a REJECT installs nothing. (Uses a copy of the orders spec with another description,
     then re-installs the original from datasets/orders.json.)
  E. One real Spec Writer run proposes a spec for orders from the profile: it is saved as a
     proposal, it is valid, and it is NOT installed. Set SKIP_LLM=1 to skip (no Bedrock cost).

Usage (from app/EtlCopilot/):
    uv run python ../../tests/test_specs.py
"""
import copy
import json
import os
import re
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(os.getenv("ETL_COPILOT_APP", ROOT / "app" / "EtlCopilot"))
sys.path.insert(0, str(APP_DIR))

import approvals  # noqa: E402
from agents.script_writer import script_writer  # noqa: E402
from agents.spec_writer import EXAMPLE, spec_writer  # noqa: E402
from config import BUCKET, CONTRACTS_PREFIX, REGION, SPEC_DRAFTS_PREFIX  # noqa: E402
from tools.drafts import check_script, dataset_of_script  # noqa: E402
from tools.specs import (check_spec, list_datasets, load_spec, read_spec_draft, save_spec_draft,  # noqa: E402
                         seed_spec, spec_id_for)

ORDERS_FILE = APP_DIR / "datasets" / "orders.json"
ORDERS = json.loads(ORDERS_FILE.read_text(encoding="utf-8"))
admin_s3 = boto3.client("s3", region_name=REGION)  # only for test setup/cleanup, never by the agent
results = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def contract_now() -> dict:
    return load_spec("orders")


print("--- A. specs and scripts are checked (offline) ---")
check("orders.json is a valid spec", not check_spec(ORDERS), str(check_spec(ORDERS)[:1]))
check("the Spec Writer's example is a valid spec", not check_spec(EXAMPLE), str(check_spec(EXAMPLE)[:1]))
for name, mutate in [
    ("path traversal in dataset", lambda s: s.update(dataset="../orders")),
    ("SQL in a column name", lambda s: s["columns"].append({"name": 'x"; DROP TABLE y; --', "type": "string"})),
    ("SQL in a type", lambda s: s["columns"][0].update(type="string) ; DROP")),
    ("a stored path", lambda s: s.update(landing_prefix="landing/support_tickets/")),
    ("rule on a missing column", lambda s: s["rules"].append({"id": "R8_x", "type": "matches", "column": "nope", "pattern": "^a$"})),
    ("invalid regex", lambda s: s["rules"].append({"id": "R8_x", "type": "matches", "column": "currency", "pattern": "([A-Z"})),
    ("key not in the contract", lambda s: s.update(primary_key=["nope"])),
]:
    bad = copy.deepcopy(ORDERS)
    mutate(bad)
    check(f"rejected: {name}", bool(check_spec(bad)))

golden = (ROOT / "etl" / "etl-copilot-job.py").read_text(encoding="utf-8")
check("golden script belongs to orders and passes", dataset_of_script(golden) == "orders"
      and not check_script(golden, "orders"))
check("the same script is rejected for another dataset", bool(check_script(golden, "inventory_snapshot")))
leak = golden + '\nOTHER = "s3://etl-copilot-ak/staging/support_tickets/"\n'
check("a script touching another dataset's folder is rejected",
      any("outside this dataset" in p for p in check_script(leak, "orders")))

print("--- B. the installed orders contract ---")
check("installed contract == datasets/orders.json", contract_now() == ORDERS)
rows = {d["dataset"]: d for d in list_datasets()}
check("list_datasets: orders has a landing folder, raw table and spec",
      rows.get("orders") == {"dataset": "orders", "landing_folder": True, "raw_table": True, "approved_spec": True},
      str(rows.get("orders")))

print("--- C. no spec, no script; no data, no spec ---")
reply = script_writer("nospec_test", "Write the ETL.")
check("script_writer refuses a dataset without a spec", reply.startswith("NO_SPEC"), reply[:70])
reply = spec_writer("nodata_test", "Propose a spec.")
check("spec_writer refuses a dataset without raw data", reply.startswith("NO_DATA"), reply[:70])

print("--- D. only an exact APPROVE SPEC installs a proposal ---")
variant = copy.deepcopy(ORDERS)
variant["description"] = "TEST COPY of the orders contract (step 4.2.2 test)"
saved = save_spec_draft(variant)
spec_id = saved.get("spec_id", "")
check("a proposal is saved under its content hash", saved["saved"] and spec_id == spec_id_for(variant), str(saved))
check("saving a proposal does not install it", contract_now() == ORDERS)

for text in (f"Please APPROVE SPEC {spec_id}", f"APPROVE SPEC {spec_id} now", f"approve spec: {spec_id}"):
    approvals.handle_human_message(text)
    check(f"{text!r} installs nothing", contract_now() == ORDERS)
msg = approvals.handle_human_message("APPROVE SPEC 000000000000")
check("APPROVE SPEC with an unknown id installs nothing", "could not be installed" in msg and contract_now() == ORDERS)
msg = approvals.handle_human_message(f"REJECT SPEC {spec_id} key should include customer_id")
check("REJECT SPEC passes the reason on, installs nothing",
      "REJECTED" in msg and "customer_id" in msg and contract_now() == ORDERS)

tampered = copy.deepcopy(variant)
tampered["description"] = "TEST COPY, tamper case"
tampered_id = save_spec_draft(tampered)["spec_id"]
evil = copy.deepcopy(tampered)
evil["rules"] = []  # someone strips the validation rules after the engineer reviewed it
admin_s3.put_object(Bucket=BUCKET, Key=f"{SPEC_DRAFTS_PREFIX}{tampered_id}.json", Body=json.dumps(evil).encode())
msg = approvals.handle_human_message(f"APPROVE SPEC {tampered_id}")
check("a proposal changed after review is not installed", "changed after it was proposed" in msg
      and contract_now() == ORDERS, msg[:90])

msg = approvals.handle_human_message(f"  approve spec {spec_id.upper()}  ")
check("the exact command installs the reviewed proposal", "APPROVED spec" in msg
      and contract_now() == variant, msg[:90])
seed_spec(str(ORDERS_FILE))
check("original orders contract re-installed", contract_now() == ORDERS)
for sid in (spec_id, tampered_id):
    admin_s3.delete_object(Bucket=BUCKET, Key=f"{SPEC_DRAFTS_PREFIX}{sid}.json")

if os.getenv("SKIP_LLM"):
    print("\nSKIP_LLM set: skipping the real Spec Writer run")
else:
    print("--- E. a real Spec Writer proposal (1-2 minutes) ---")
    reply = spec_writer("orders", "Propose the spec for the orders export from its profile.")
    print("    " + "\n    ".join(reply.splitlines()[:40]))
    match = re.search(r"SPEC_ID:\W*([0-9a-f]{12})", reply)  # tolerates **bold** and `code` markup
    check("reply has a SPEC_ID", bool(match))
    if match:
        proposal = read_spec_draft(match.group(1))
        check("the proposal is a valid spec", not check_spec(proposal), str(check_spec(proposal)[:2]))
        check("the proposal was NOT installed", contract_now() == ORDERS)
        mine = {c["name"] for c in ORDERS["columns"]}
        theirs = {c["name"] for c in proposal["columns"]}
        print(f"INFO  proposed {len(theirs)} columns; {len(mine & theirs)}/{len(mine)} match your contract's names")
        print(f"INFO  PII handled: {[(p['column'], p['treatment']) for p in proposal.get('pii', [])]}")
        print(f"INFO  rules: {[r['id'] for r in proposal.get('rules', [])]}")

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
