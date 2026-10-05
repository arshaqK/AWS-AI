"""Step 5.1 gate (offline part): the console's narration events.

  A. Each event is built from real results and matches AWS: the orders profile (answer
     key), the approved spec, draft 79a94743fde0, its Glue run and today's validation.
  B. No event carries personal data, for all three datasets' profiles.
  C. A gate only appears for something this call really saved: a reply that names an
     unsaved spec id, or another dataset's draft, produces no gate.
  D. The channel: nothing is sent outside a request; events posted from worker threads
     arrive in order; a dataset's profile is narrated once per request.
  E. The entrypoint streams narration in order with the text and ends with a final event.

Read-only against AWS (a few Athena queries). Usage (from app/EtlCopilot/):
    uv run python ../../tests/test_narration.py
"""
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(os.getenv("ETL_COPILOT_APP", ROOT / "app" / "EtlCopilot"))
sys.path.insert(0, str(APP_DIR))

import boto3  # noqa: E402

import narration  # noqa: E402
from config import JOB_NAME, REGION  # noqa: E402
from tools.drafts import read_confirmations, read_draft  # noqa: E402
from tools.execution import validate_output  # noqa: E402
from tools.pii import find_personal_data  # noqa: E402
from tools.profile import build_profile  # noqa: E402
from tools.specs import load_spec, paths, read_spec_draft, spec_id_for  # noqa: E402

results = []
KEY = json.loads((ROOT / "data" / "orders_export.expected.json").read_text())
DRAFT, RUN = "79a94743fde0", "jr_ceaabb0c526d26dc20f1a1b5efa286310acb973ec9688854057de16a8d0dce32"


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def text_of(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False)


print("--- A. events match AWS ---")
profiles = {d: build_profile(d) for d in ("orders", "inventory_snapshot", "support_tickets")}
step = narration.profile_step(profiles["orders"])
check("profile: row and column counts from the answer key",
      f"**{KEY['rows_in_file']} rows, {len(KEY['columns'])} columns**" in step["bullets"][0], step["bullets"][0])
check("profile: order_date's formats, customer_email as personal data, null spellings",
      any(b.startswith("`order_date`: ") and "formats" in b for b in step["bullets"])
      and any("`customer_email`" in b and "personal data" in b for b in step["bullets"])
      and any(b.startswith("null spellings") and "`N/A`" in b for b in step["bullets"]), str(step["bullets"]))
inv = narration.profile_step(profiles["inventory_snapshot"])
check("profile: inventory's weight units and negative on_hand surface",
      any("units" in b and "`oz`" in b for b in inv["bullets"]) and any("negative" in b for b in inv["bullets"]),
      str([b for b in inv["bullets"] if "units" in b or "negative" in b]))

spec = load_spec("orders")
reused = narration.spec_reused_step("orders", spec_id_for(spec), spec, ["x"])
check("spec in use: the approved spec's id", spec_id_for(spec) == "5a69b40141ba" and "5a69b40141ba" in reused["text"])
proposal = read_spec_draft("dbc49fa5509a")
gate = narration.spec_gate("dbc49fa5509a", proposal, ["a point"])
check("spec gate: id, dataset, key and full contract from the saved proposal",
      gate["id"] == "dbc49fa5509a" and gate["dataset"] == "inventory_snapshot"
      and json.loads(gate["code"]) == proposal and all(f"`{k}`" in gate["summary"][0] for k in proposal["primary_key"]))

script = read_draft(DRAFT)
reply = (f"DRAFT_ID: {DRAFT}\nWhat the script does:\n- parses three date formats\n- hashes email and phone\n"
         "Assumptions:\n- none")
step, gate = narration.draft_step_and_gate("orders", DRAFT, script, reply,
                                           read_confirmations(DRAFT).get("needs_confirmation", []),
                                           paths("orders"), spec["primary_key"])
check("draft gate: id, the exact script in S3, the reply's bullets",
      gate["id"] == DRAFT and gate["code"] == script and gate["summary"] == ["parses three date formats", "hashes email and phone"])
real_reply = ("DRAFT_ID: abc\n**What the script does:**\n- `ticket_id`: validated\n- `created_at`: parsed to UTC\n"
              "**Assumptions:**\n- none\n**Changed since 111111111111:**\n- kept `notes`")
check("a bold heading ends the plan; 'none' is never a bullet (seen in a real draft)",
      narration.section(real_reply, "What the script does") == ["`ticket_id`: validated", "`created_at`: parsed to UTC"]
      and narration.section(real_reply, "Changed since") == ["kept `notes`"])
check("draft gate: guardrails name the dataset's staging folder and key",
      "s3://etl-copilot-ak/staging/orders/" in gate["guardrails"][0] and "`order_id`" in gate["guardrails"][2])

run = boto3.client("glue", region_name=REGION).get_job_run(JobName=JOB_NAME, RunId=RUN)["JobRun"]
check("run step: Glue's state and duration", f"**{run['JobRunState']}** in {run['ExecutionTime']} s"
      in narration.run_step(run, DRAFT)["text"])
validation = validate_output("orders")
vstep = narration.validation_step(validation, 1)
check("validation step: every rule, 7/7 passed", "**7/7 passed** (attempt 1)" in vstep["text"]
      and all(name in vstep["text"] for name in validation["rules"]) and vstep["passed"] is True)

print("--- B. no personal data in any event ---")
for d, p in profiles.items():
    hits = find_personal_data(text_of(narration.profile_step(p)))
    check(f"profile event for {d}: no email or phone", not hits, str(hits[:2]))
check("draft step: no email or phone", not find_personal_data(text_of(step)))

print("--- C. gates only for what this call saved ---")
from agents import script_writer as sw  # noqa: E402
from agents import spec_writer as spw  # noqa: E402

posted = []
narration.post, real_post = posted.append, narration.post
spw._narrate_gate("SPEC_ID: dbc49fa5509a\n...", saved=[])
check("a spec id this call did not save: no gate", posted == [])
spw._narrate_gate("SPEC_ID: dbc49fa5509a\nNEEDS CONFIRMATION:\n- **on_hand**: negatives are backorders\n- nothing",
                  saved=["dbc49fa5509a"])
check("a spec this call saved: gate with its points (markdown stripped, 'nothing' dropped)",
      len(posted) == 1 and posted[0]["needs_confirmation"] == ["on_hand: negatives are backorders"], str(posted[:1]))
sw.save_confirmations = lambda *a: None  # never touch the real draft's points file here
check("another dataset's draft id: nothing kept, so no gate",
      sw._keep_confirmations("support_tickets", f"DRAFT_ID: {DRAFT}") is None)
check("an id with no saved draft: no gate", sw._keep_confirmations("orders", "DRAFT_ID: 000000000000") is None)
narration.post = real_post

print("--- D. the channel ---")
narration.post({"type": "step"})  # no request open: must be a silent no-op
check("posting outside a request does nothing", narration._channel() is None)


async def channel_order():
    got = []
    loop = asyncio.get_running_loop()
    channel = narration.open_channel(loop, got.append)

    def worker():
        for i in range(5):
            narration.post({"type": "step", "n": i})
        narration.speaking("script")
        got.append(("first", narration.first_profile("orders"), narration.first_profile("orders")))
        got.append(("route", narration.route("profile")))

    t = threading.Thread(target=worker)  # a plain thread does not inherit the context
    t.start()
    await asyncio.to_thread(t.join)
    await asyncio.sleep(0.05)
    narration.close_channel(channel)
    return got


got = asyncio.run(channel_order())
steps = [g["narration"]["n"] for g in got if isinstance(g, dict)]
check("events from a worker thread arrive, in order", steps == [0, 1, 2, 3, 4], str(steps))
check("a dataset is profiled-narrated once per request", ("first", True, False) in got)
check("routes name the specialist that asked", ("route", "Script Writer → Profile") in got)
check("closing the channel stops delivery", narration._channel() is None)

print("--- E. the entrypoint stream ---")
import main  # noqa: E402


class FakeAgent:
    async def stream_async(self, prompt):
        yield {"event": {"contentBlockDelta": {"delta": {"text": "Working on "}}}}
        await asyncio.to_thread(narration.post, {"type": "step", "agent": "profile", "text": "scanned"})
        yield {"event": {"contentBlockDelta": {"delta": {"text": "it."}}}}


main.get_or_create_agent = lambda session_id: FakeAgent()


async def collect():
    return [e async for e in main.invoke({"prompt": "hello"}, type("Ctx", (), {"session_id": "s"})())]


events = asyncio.run(collect())
kinds = ["text" if "event" in e else e["narration"]["type"] if "narration" in e else "other" for e in events]
check("narration arrives between the text it happened between", kinds == ["text", "step", "text", "final"], str(kinds))
check("the final event carries the whole reply", events[-1]["narration"]["text"] == "Working on it.")

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
