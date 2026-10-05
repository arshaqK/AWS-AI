"""Steps 5.2-5.3 gate (offline): the real console app, driven headlessly with a fake agent and S3.

  A. Upload checks: only comma-separated UTF-8 CSV with a proper header reaches S3.
  B. Upload -> 'Onboard <dataset>' -> narration -> the contract card; its buttons send the
     exact commands; only the open card is clickable; the repeated reply is dropped.
  C. Request changes, a failed run that is revised, approve to Passed (one result card),
     a plain reject, an agent error, typed commands, a new session.
  D. A file next to another file in the same landing folder needs an explicit OK.

No AWS calls. Run from etl-copilot/ui:  uv run python tests/test_app.py
"""
import os
import re
import sys
import time
from pathlib import Path

os.environ["MEMORY_ETLCOPILOTMEMORY_ID"] = ""  # conventions off: no AWS in this test
UI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(UI))

from streamlit.testing.v1 import AppTest  # noqa: E402

import upload  # noqa: E402

results = []


def check(name, passed, detail=""):
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def refused(name, data):
    try:
        upload.check_csv(name, data)
        return ""
    except upload.UploadError as e:
        return str(e)


print("--- A. upload checks ---")
good = b"id,name\n1,a\n2,b\n"
check("a proper CSV passes", upload.check_csv("t.csv", good) == {"rows": 2, "columns": ["id", "name"]})
check("an .xlsx name is refused", "not a .csv" in refused("t.xlsx", good))
check("a binary file renamed .csv is refused", "binary" in refused("t.csv", b"PK\x03\x04\x00\x00junk"))
check("non-UTF-8 text is refused", "UTF-8" in refused("t.csv", "id,name\n1,caf\xe9\n".encode("latin-1")))
check("semicolon-separated is refused", "comma-separated" in refused("t.csv", b"id;name\n1;a\n"))
check("a repeated column name is refused", "repeats" in refused("t.csv", b"id,id\n1,2\n"))
check("a header without rows is refused", "no data rows" in refused("t.csv", b"id,name\n"))
check("an empty file is refused", "empty" in refused("t.csv", b""))
check("dataset names follow the file name",
      [upload.dataset_from_filename(n) for n in ("support_tickets.csv", "Q3 Orders-Export.CSV", "2025 sales.csv")]
      == ["support_tickets", "q3_orders_export", "d_2025_sales"])
specs_rule = re.search(r'DATASET_RE = re.compile\(r"([^"]+)"\)',
                       (UI.parent / "app" / "EtlCopilot" / "tools" / "specs.py").read_text()).group(1)
check("the console's dataset rule is the pipeline's rule", upload.DATASET_RE.pattern == specs_rule)


class FakeS3:
    def __init__(self, existing=()):
        self.objects = {k: b"x" for k in existing}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def get_paginator(self, _):
        objects = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}]
        return P()


def gate(kind, gid, changed=()):
    return {"type": "gate", "kind": kind, "id": gid, "dataset": "orders_demo", "title": "Ready for your approval",
            "summary": ["`order_date`: three formats parsed"], "changed": list(changed), "needs_confirmation": [],
            "guardrails": ["writes only to `staging/`"], "code": "print(1)"}


def draft(gid):
    return {"type": "step", "agent": "script", "phase": "draft", "text": f"Drafted the ETL — `{gid}`, 1 steps."}


FINAL = {"type": "final", "text": "Long Supervisor reply that repeats the card."}
PASS_RUN = [
    {"type": "step", "agent": "execution", "phase": "run_started", "text": "Started Glue run."},
    {"type": "step", "agent": "execution", "phase": "run", "state": "SUCCEEDED", "text": "SUCCEEDED"},
    {"type": "step", "agent": "execution", "phase": "validate", "passed": True, "dataset": "orders_demo",
     "rules_passed": 7, "rules_total": 7, "attempt": 2, "text": "**7/7 passed**"},
    {"type": "step", "agent": "documenter", "phase": "docs", "text": "Wrote the docs",
     "files": {"data_dictionary": "s3://b/dd.md", "quality_report": "s3://b/qr.md",
               "clean_csv": "s3://b/orders_demo.csv"}},
    FINAL,
]
SCRIPT = {  # the fake agent's replies, keyed by the exact message it receives
    "Onboard orders_demo": [{"type": "step", "agent": "profile", "phase": "profile", "text": "Scanned."},
                            gate("spec", "aaaaaaaaaaaa"), FINAL],
    "Onboard known": [{"type": "step", "agent": "spec", "phase": "spec", "text": "Using the contract."},
                      {"type": "step", "agent": "profile", "phase": "profile", "text": "Scanned."},
                      draft("ffffffffffff"), gate("draft", "ffffffffffff"), FINAL],
    "APPROVE SPEC aaaaaaaaaaaa": [draft("bbbbbbbbbbbb"), gate("draft", "bbbbbbbbbbbb"), FINAL],
    "REJECT bbbbbbbbbbbb keep customer_name, it's a company name, parse slash dates as DD/MM, drop the notes column, uppercase currency": [draft("cccccccccccc"),
                                               gate("draft", "cccccccccccc", ["`customer_name` kept"]), FINAL],
    "APPROVE cccccccccccc": [
        {"type": "step", "agent": "execution", "phase": "run_started", "text": "Started."},
        {"type": "step", "agent": "execution", "phase": "run", "state": "FAILED", "text": "FAILED"},
        draft("dddddddddddd"), gate("draft", "dddddddddddd", ["timestamps parsed as UTC"]), FINAL],
    "APPROVE dddddddddddd": PASS_RUN,
    "REJECT eeeeeeeeeeee": [{"type": "final", "text": "Rejected — nothing ran."}],
}


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.prompts = []

    def healthy(self):
        return True

    def stream(self, prompt, session_id):
        self.prompts.append((prompt, session_id))
        if prompt.startswith("boom"):
            raise ConnectionError("agent unreachable")
        for ev in SCRIPT.get(prompt, [{"type": "final", "text": "ok"}]):
            time.sleep(0.03)
            yield {"type": "heartbeat"}
            yield ev


def app(existing=()):
    at = AppTest.from_file(str(UI / "app.py"), default_timeout=60)
    fake, s3 = FakeBackend(), FakeS3(existing)
    at.session_state["fake_backend"], at.session_state["s3_override"] = fake, s3
    at.session_state["test_upload"] = ("orders_demo.csv", (UI.parent / "data" / "orders_export.csv").read_bytes())
    at.run()
    return at, fake, s3


def settle(at, timeout=20):
    """The console polls with a timed fragment, which AppTest does not tick: rerun by hand."""
    end = time.time() + timeout
    while at.session_state["turn"] is not None and time.time() < end:
        time.sleep(0.05)
        at.run()


def button(at, start):
    return next(b for b in at.button if b.label.startswith(start))


def live_cards(at):
    return sorted(b.key.split("-", 1)[1] for b in at.button if b.key and b.key.startswith("approve-"))


def kinds(at):
    return [i["type"] + ":" + i.get("phase", i.get("kind", "")) for i in at.session_state["feed"]]


print("--- B. upload -> contract card -> buttons ---")
at, fake, s3 = app()
check("no errors on load; the file is checked", not at.exception and any("508 rows" in s.value for s in at.success))
button(at, "Upload & onboard orders_demo").click().run()
settle(at)
check("the CSV landed in landing/orders_demo/", "landing/orders_demo/orders_demo.csv" in s3.objects)
check("after the upload, the uploader, name box and button are gone",
      not at.text_input and not any(b.label.startswith("Upload & onboard") for b in at.button))
check("the agent was asked to onboard it", fake.prompts[0][0] == "Onboard orders_demo" and len(fake.prompts[0][1]) >= 33)
check("the turn ends at the contract card; the repeated reply is dropped",
      kinds(at) == ["user:", "step:profile", "gate:spec"] and at.session_state["stage"] == "gate_spec", str(kinds(at)))
check("only the open card has buttons", live_cards(at) == ["aaaaaaaaaaaa"])
button(at, "Approve contract").click().run()
settle(at)
check("Approve contract sends exactly APPROVE SPEC <id>", fake.prompts[-1][0] == "APPROVE SPEC aaaaaaaaaaaa")
check("the decided card closes; the draft card is the only live one",
      at.session_state["decided"].get("aaaaaaaaaaaa", "").startswith("✅") and live_cards(at) == ["bbbbbbbbbbbb"])

print("--- C. request changes, a failed run, approve, reject ---")
at.toggle(key="request-open-bbbbbbbbbbbb").set_value(True).run()
settle(at)
at.text_area(key="reason-bbbbbbbbbbbb").set_value("keep customer_name, it's a company name, parse slash dates as DD/MM, drop the notes column, uppercase currency").run()
settle(at)
button(at, "Send to the agent").click().run()
settle(at)
check("Request changes sends the whole multi-part request, word for word",
      fake.prompts[-1][0] == "REJECT bbbbbbbbbbbb keep customer_name, it's a company name, parse slash dates as DD/MM, drop the notes column, uppercase currency", fake.prompts[-1][0])
check("the old card says what you asked; the revised card is live",
      at.session_state["decided"]["bbbbbbbbbbbb"] == "↺ Changes requested: keep customer_name, it's a company name, parse slash dates as DD/MM, drop the notes column, uppercase currency"
      and live_cards(at) == ["cccccccccccc"])
button(at, "Approve & run").click().run()
settle(at)
check("a failed run: the approved card stays approved, the revision is live, the rail resets",
      at.session_state["decided"]["cccccccccccc"].startswith("✅") and live_cards(at) == ["dddddddddddd"]
      and at.session_state["rail"].get("Run") == "pending" and at.session_state["stage"] == "gate_draft",
      str(at.session_state["rail"]))
button(at, "Approve & run").click().run()
settle(at)
steps = ["Profile", "Spec", "Draft", "Approval", "Run", "Validate", "Document"]
check("approve runs to Passed, every step done", at.session_state["stage"] == "done"
      and all(at.session_state["rail"].get(s) == "done" for s in steps), str(at.session_state["rail"]))
check("the passing run ends in one result card, never the long reply",
      at.session_state["feed"][-1]["type"] == "result" and at.session_state["feed"][-1]["rules_passed"] == 7
      and not any(i["type"] == "final" for i in at.session_state["feed"]))
check("the result card offers the clean CSV", any(d.label == "Download orders_demo.csv"
                                                 for d in at.get("download_button")))
session = fake.prompts[0][1]
check("every message went to the same session", {sid for _, sid in fake.prompts} == {session})

at.session_state["feed"].append(gate("draft", "eeeeeeeeeeee"))
at.session_state["gate_id"], at.session_state["stage"] = "eeeeeeeeeeee", "gate_draft"
at.session_state["rail"]["Approval"] = "waiting"
at.run()
button(at, "Reject").click().run()
settle(at)
check("Reject sends exactly REJECT <id> and ends Rejected", fake.prompts[-1][0] == "REJECT eeeeeeeeeeee"
      and at.session_state["stage"] == "rejected" and at.session_state["rail"].get("Approval") == "failed")
check("a reply that is not a card is shown as is",
      at.session_state["feed"][-1] == {"type": "final", "text": "Rejected — nothing ran."})
at.chat_input[0].set_value("boom").run()
settle(at)
check("an agent error is shown in the feed, not raised", not at.exception and at.session_state["stage"] == "error"
      and at.session_state["feed"][-1]["type"] == "error")
at.chat_input[0].set_value("approve 0123456789ab").run()
settle(at)
check("a typed command closes its card too", at.session_state["decided"].get("0123456789ab", "").startswith("✅"))
button(at, "New session").click().run()
settle(at)
check("New session starts over with a new id", at.session_state["stage"] == "upload" and not at.session_state["feed"]
      and at.session_state["session_id"] != session)

import backend  # noqa: E402


class Talkative:
    def stream(self, prompt, session_id):
        yield {"type": "activity", "agent": "script", "text": "Script Writer is writing the ETL script for x"}
        time.sleep(0.3)
        yield {"type": "final", "text": "done"}


turn = backend.start_turn(Talkative(), "go", "s" * 40)
time.sleep(0.15)
check("the status line says what the agent is doing", turn.activity.startswith("Script Writer is writing")
      and turn.activity_agent == "script")
time.sleep(0.4)
check("activity is not added to the feed", [e["type"] for e in turn.events] == ["final"])

at, fake, s3 = app()
at.session_state["stage"], at.session_state["session_id"] = "ready", "x" * 40
at.run()
at.chat_input[0].set_value("Onboard known").run()
settle(at)
check("a profile that arrives after the contract step never un-finishes Spec (seen live)",
      at.session_state["rail"].get("Spec") == "done" and at.session_state["rail"].get("Profile") == "done",
      str(at.session_state["rail"]))

print("--- D. another file in the same folder ---")
at, fake, s3 = app(existing=["landing/orders_demo/older_export.csv"])
check("a warning names the other file", any("older_export.csv" in w.value for w in at.warning))
check("upload is blocked until you agree", button(at, "Upload & onboard").disabled)
at.checkbox(key="agree_others").check().run()
settle(at)
check("after agreeing it can upload", not button(at, "Upload & onboard").disabled)

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
