"""Clicks through every path of the static mock headlessly (Streamlit AppTest).

Run from etl-copilot/ui:  uv run python tests/test_mock_app.py
"""
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

UI = Path(__file__).resolve().parents[1]
results = []


def check(name, passed, detail=""):
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def app(scenario="known"):
    at = AppTest.from_file(str(UI / "mock_app.py"), default_timeout=60)
    at.session_state["fast"] = True
    at.session_state["scenario"] = scenario
    at.run()
    return at


def click(at, label_start):
    btn = next(b for b in at.button if b.label.startswith(label_start))
    btn.click().run()
    return at


def kinds(at):
    return [e["type"] + ":" + e.get("agent", e.get("kind", "")) for e in at.session_state["events"]]


print("--- known dataset: one approval ---")
at = app()
check("starts on the upload screen, no errors", at.session_state["stage"] == "upload" and not at.exception)
click(at, "Use the sample")
check("plays up to the draft gate by itself", at.session_state["stage"] == "gate_draft"
      and at.session_state["gate_id"] == "79a94743fde0", str(kinds(at)))
check("profile → spec (reused) → script → gate, no contract gate",
      kinds(at) == ["user:", "step:supervisor", "step:profile", "step:spec", "step:script", "gate:draft"])
check("rail: Approval waits for you", at.session_state["rail"].get("Approval") == "waiting")
click(at, "Approve & run")
check("approve runs to Passed", at.session_state["stage"] == "done" and not at.exception)
check("rail all done", all(at.session_state["rail"].get(s) == "done"
                           for s in ["Profile", "Spec", "Draft", "Approval", "Run", "Validate", "Document"]))
check("the confirmed point is remembered", any("phone" in p for p in at.session_state["prefs"]))
check("the user's command shown exactly", any(e.get("text") == "APPROVE 79a94743fde0" for e in at.session_state["events"]))

print("--- a note that changes the plan, then a note that changes nothing ---")
at = app()
click(at, "Use the sample")
at.chat_input[0].set_value("keep customer_name, it's a company name").run()
new_id = at.session_state["gate_id"]
check("note → Script Writer revises, new draft id", at.session_state["stage"] == "gate_draft" and new_id != "79a94743fde0")
diff = next(e for e in reversed(at.session_state["events"]) if e.get("diff"))["diff"]
check("diff shows the change", [s for s, _ in diff] == ["-", "+"] and "keep `customer_name`" in diff[1][1])
old_card = next(e for e in at.session_state["events"] if e.get("id") == "79a94743fde0")
check("the old card still shows the old plan", any("drop `customer_name`" in s for s in old_card["summary"]))
click(at, "Remember for orders")
check("Remember adds the note", "keep customer_name, it's a company name" in at.session_state["prefs"])
before = len(at.session_state["events"])
at.chat_input[0].set_value("use DD/MM for slash dates").run()
check("a note that changes nothing keeps the same draft", at.session_state["gate_id"] == new_id
      and not at.session_state["events"][-1].get("diff"), str(len(at.session_state["events"]) - before))

print("--- request changes, reject ---")
at = app()
click(at, "Use the sample")
at.toggle(key="request-open-79a94743fde0").set_value(True).run()
at.text_area(key="reason-79a94743fde0").set_value("treat N/A in discount_code as no discount").run()
click(at, "Send to the agent")
check("Request changes sends REJECT <id> <reason> and revises",
      any(e.get("text", "").startswith("REJECT 79a94743fde0 treat") for e in at.session_state["events"])
      and at.session_state["gate_id"] not in (None, "79a94743fde0"))
click(at, "Reject")
check("Reject: nothing runs", at.session_state["stage"] == "rejected"
      and at.session_state["rail"].get("Run") is None and at.session_state["rail"].get("Approval") == "failed")

print("--- new dataset: contract, then script ---")
at = app("new")
click(at, "Use the sample")
check("stops at the contract gate first", at.session_state["stage"] == "gate_spec"
      and at.session_state["gate_id"] == "5a69b40141ba")
click(at, "Approve contract")
check("then drafts and stops at the script gate", at.session_state["stage"] == "gate_draft")
click(at, "Approve & run")
check("then runs to Passed", at.session_state["stage"] == "done" and not at.exception)

print("--- forget, start over ---")
n = len(at.session_state["prefs"])
at.button(key="forget-0").click().run()
check("✕ forgets a preference", len(at.session_state["prefs"]) == n - 1)
click(at, "Start over")
check("Start over returns to upload", at.session_state["stage"] == "upload" and not at.session_state["events"])

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
