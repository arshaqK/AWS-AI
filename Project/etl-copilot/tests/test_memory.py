"""Step 4.1 gate (offline part): conventions memory, with a stand-in for AgentCore Memory.

  A. Off until deployed: without MEMORY_ETLCOPILOTMEMORY_ID nothing is saved.
  B. Only the engineer's exact REMEMBER / FORGET commands change memory; the list is the
     replay of remember/forget events; duplicates and bad numbers change nothing.
  C. APPROVE <draft> remembers exactly that draft's recorded NEEDS CONFIRMATION points,
     and a memory failure never blocks the approval itself.
  D. The Script Writer receives the remembered conventions, and its reply's NEEDS
     CONFIRMATION lines are recorded beside the draft by code.
  E. (live IAM, read-only) The Execution role - the one the models' tools use - cannot
     write to AgentCore Memory.

No AWS resources are created. Usage (from app/EtlCopilot/):
    uv run python ../../tests/test_memory.py
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(os.getenv("ETL_COPILOT_APP", Path(__file__).resolve().parents[1] / "app" / "EtlCopilot"))
sys.path.insert(0, str(APP_DIR))

import boto3  # noqa: E402

import approvals  # noqa: E402
import memory  # noqa: E402
from agents import script_writer as sw  # noqa: E402
from config import EXECUTION_ROLE_ARN, REGION  # noqa: E402

results = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


class FakeMemory:
    """Just enough of the bedrock-agentcore data plane: create_event / list_events."""
    def __init__(self, fail=False):
        self.events, self.fail = [], fail

    def create_event(self, memoryId, actorId, sessionId, eventTimestamp, payload):
        if self.fail:
            raise RuntimeError("memory service unavailable")
        self.events.append({"memoryId": memoryId, "actorId": actorId, "sessionId": sessionId,
                            "eventTimestamp": datetime.now(timezone.utc), "payload": payload})

    def list_events(self, memoryId, actorId, sessionId, includePayloads, maxResults, nextToken=None):
        return {"events": [e for e in self.events if (e["actorId"], e["sessionId"]) == (actorId, sessionId)]}


print("--- A. off until deployed ---")
os.environ.pop(memory.MEMORY_ENV, None)
check("memory disabled without the env var", not memory.enabled())
check("remember is a no-op", memory.remember("orders", "x", "test") is False and memory.conventions("orders") == [])
check("REMEMBER says nothing was saved",
      "not deployed" in approvals.handle_human_message("REMEMBER orders: slash dates are DD/MM/YYYY"))

print("--- B. only exact commands change memory ---")
os.environ[memory.MEMORY_ENV] = "fakememory-123"
memory._client = FakeMemory()
msg = approvals.handle_human_message("REMEMBER orders: 1900-01-01 means an unknown date")
check("REMEMBER saves", memory.conventions("orders") == ["1900-01-01 means an unknown date"], msg[:70])
approvals.handle_human_message("remember   ORDERS :  slash dates are DD/MM/YYYY ")
check("case and spacing are tolerated", memory.conventions("orders")[-1] == "slash dates are DD/MM/YYYY")
for text in ("Please REMEMBER orders: x", "REMEMBER orders x", "I want you to remember orders: y",
             '"REMEMBER orders: z"', "REMEMBER ../orders: z"):
    approvals.handle_human_message(text)
check("non-exact commands save nothing", len(memory.conventions("orders")) == 2, str(memory.conventions("orders")))
approvals.handle_human_message("REMEMBER orders: 1900-01-01 means an unknown date")
check("a duplicate is not added twice", len(memory.conventions("orders")) == 2)
check("conventions are per dataset", memory.conventions("support_tickets") == [])
msg = approvals.handle_human_message("FORGET orders 1")
check("FORGET removes the n-th", memory.conventions("orders") == ["slash dates are DD/MM/YYYY"], msg[:80])
approvals.handle_human_message("FORGET orders 9")
check("FORGET with a bad number removes nothing", memory.conventions("orders") == ["slash dates are DD/MM/YYYY"])
memory.remember("orders", "1900-01-01 means an unknown date", "REMEMBER")
check("forget then remember again restores it (replay order)", len(memory.conventions("orders")) == 2)
events = memory.history("orders")
check("every change is kept as an auditable event", [e["op"] for e in events] == ["remember", "remember", "forget", "remember"])
check("a convention containing personal data is refused",
      memory.remember("orders", "treat liam.wang27@outlook.com as a test account", "REMEMBER") is False
      and memory.remember("orders", "the number 415.555.0142 is a test line", "REMEMBER") is False)
check("events are filed under the dataset's actor", {e["actorId"] for e in memory._client.events} == {"dataset-orders"})

print("--- C. APPROVE confirms exactly the draft's recorded points ---")
memory._client = FakeMemory()
points = ["order_date: 1900-01-01 (3 rows) -> treated as null", "phone: numbers without + use ship_country's code"]
approvals.read_confirmations = lambda d: {"dataset": "orders", "needs_confirmation": points} if d == "abcdef123456" else {}
msg = approvals.handle_human_message("APPROVE abcdef123456")
check("approval recorded", "abcdef123456" in approvals.pending())
check("its NEEDS CONFIRMATION points are remembered", memory.conventions("orders") == points, msg[-110:])
check("source is the approval", {e["source"] for e in memory.history("orders")} == {"APPROVE abcdef123456"})
approvals.consume("abcdef123456")
approvals.handle_human_message("APPROVE 000000000000")
check("a draft with no recorded points remembers nothing", memory.conventions("orders") == points)
approvals.consume("000000000000")
memory._client = FakeMemory(fail=True)
msg = approvals.handle_human_message("APPROVE abcdef123456")
check("memory failure does not block the approval", "abcdef123456" in approvals.pending()
      and "could not record" in msg, msg[-90:])
approvals.consume("abcdef123456")

print("--- D. the Script Writer receives conventions; its points are recorded ---")
memory._client = FakeMemory()
memory.remember("orders", "1900-01-01 means an unknown date", "REMEMBER")
captured, saved = {}, {}


class FakeAgent:
    def __init__(self, **kwargs):
        pass

    def __call__(self, task):
        captured["task"] = task
        return ("DRAFT_ID: `aaaabbbbcccc`\nWhat the script does:\n- stuff\nAssumptions:\n"
                "- NEEDS CONFIRMATION: **phone**: numbers without `+` are local to ship_country\n"
                "- NEEDS CONFIRMATION: nothing")


sw.Agent = FakeAgent
sw.load_spec = lambda d: {"columns": [{"name": "a", "type": "string"}], "primary_key": ["a"],
                          "cleaning_rules": ["a: trim"], "partition": None}
sw.read_draft = lambda d: "source"
sw.dataset_of_script = lambda s: "orders"
sw.save_confirmations = lambda d, ds, lines: saved.update(draft=d, dataset=ds, lines=lines)
sw.script_writer("orders", "Write the ETL.")
check("the task carries the remembered convention",
      "Engineer-confirmed conventions for orders" in captured["task"]
      and "1900-01-01 means an unknown date" in captured["task"])
check("the reply's points are recorded beside the draft (markdown stripped, 'nothing' skipped)",
      saved == {"draft": "aaaabbbbcccc", "dataset": "orders",
                "lines": ["phone: numbers without + are local to ship_country"]}, str(saved))
saved.clear()
sw.dataset_of_script = lambda s: "support_tickets"
sw.script_writer("orders", "Write the ETL.")
check("nothing is recorded if the named draft belongs to another dataset", saved == {})

print("--- E. the models' tools cannot write memory (IAM, read-only check) ---")
iam = boto3.client("iam")
for action in ("bedrock-agentcore:CreateEvent", "bedrock-agentcore:DeleteEvent"):
    decision = iam.simulate_principal_policy(PolicySourceArn=EXECUTION_ROLE_ARN, ActionNames=[action],
                                             ResourceArns=["*"])["EvaluationResults"][0]["EvalDecision"]
    check(f"Execution role: {action} denied", decision != "allowed", decision)

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
