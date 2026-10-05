"""Copilot Console — clickable static mock (Phase 5 design check, no AWS calls).

Plays the real orders onboarding through the same renderers the live app will use
(console_view.py). Run from etl-copilot/ui:
    uv run streamlit run mock_app.py
"""
import csv
import difflib
import hashlib
import io
import re
import time

import streamlit as st

import console_view as view
import mock_data as data

st.set_page_config(page_title="ETL Copilot Console", page_icon="🧹", layout="centered")
view.inject_theme()
S = st.session_state


def reset() -> None:
    for k in list(S.keys()):
        del S[k]


S.setdefault("stage", "upload")       # upload | working | gate_spec | gate_draft | done | rejected
S.setdefault("events", [])
S.setdefault("queue", [])             # scripted turns still to "arrive"
S.setdefault("rail", {})
S.setdefault("prefs", [data.REMEMBERED])
S.setdefault("plan", list(data.PLAN))
S.setdefault("confirm", list(data.NEEDS_CONFIRMATION))
S.setdefault("gate_id", None)
S.setdefault("closed", {})            # gate id -> note shown once it is decided
S.setdefault("file", "")
S.setdefault("rememberable", None)    # the last note that changed the plan


# ---- scripted turns ------------------------------------------------------------------
def turn(event, rail=None, stage=None, working="", then=None):
    return {"event": event, "rail": rail or {}, "stage": stage, "working": working, "then": then}


def step(agent, route="", **kw):
    return {"type": "step", "agent": agent, "route": route, **kw}


def draft_gate(draft_id):
    return turn({"type": "gate", "kind": "draft", "id": draft_id, "summary": list(S.plan),
                 "needs_confirmation": list(S.confirm), "guardrails": data.GUARDRAILS, "code": data.SCRIPT},
                rail={"Draft": "done", "Approval": "waiting"}, stage="gate_draft", working="Supervisor is preparing the approval…",
                then=("gate", draft_id))


def script_turns():
    return [turn(step("script", "Supervisor → Script Writer",
                      text=f"Drafted the ETL for `orders` — `{data.DRAFT_ID}`, {len(S.plan)} steps. Review it below."),
                 rail={"Draft": "active"}, working="Script Writer is drafting the ETL…"),
            draft_gate(data.DRAFT_ID)]


def start(file_name: str, rows: int) -> None:
    S.file, S.stage = file_name, "working"
    S.events.append({"type": "user", "text": f"📎 Uploaded **{file_name}** · {rows} rows"})
    q = [turn(step("supervisor", text="On it. I'll profile the file, check its contract, draft the cleaning, and pause "
                                      "for your approval before anything runs. Talk to me any time — I'll route your "
                                      "note to the right agent."),
              rail={"Profile": "active"}, working="Supervisor is reading your message…"),
         turn(step("profile", "Supervisor → Profile", text=f"Scanned **{file_name}** (every row, via Athena). What stood out:",
                   bullets=data.PROFILE),
              rail={"Profile": "done", "Spec": "active"}, working="Profile is scanning every row…")]
    if S.get("scenario", "known") == "known":
        q.append(turn(step("spec", "Supervisor → Spec Writer", text=data.SPEC_KNOWN,
                           bullets=[f"From memory: *{p}*" for p in S.prefs]),
                      rail={"Spec": "done"}, working="Spec Writer is looking for a contract…"))
        q += script_turns()
    else:
        q.append(turn(step("spec", "Supervisor → Spec Writer", text="`orders` is new. Proposed contract:",
                           bullets=data.SPEC_PROPOSAL),
                      working="Spec Writer is drafting the contract…"))
        q.append(turn({"type": "gate", "kind": "spec", "id": data.SPEC_ID, "title": "Approve the contract first",
                       "summary": data.SPEC_PROPOSAL, "guardrails": ["nothing runs yet",
                       "the script is drafted against this contract"], "code": data.spec_json()},
                      rail={"Spec": "waiting"}, stage="gate_spec", working="Supervisor is preparing the approval…",
                      then=("gate", data.SPEC_ID)))
    S.queue += q


# ---- the engineer's decisions (button callbacks) -------------------------------------
def close_gate(note: str) -> None:
    S.closed[S.gate_id] = note
    S.gate_id = None
    S.stage = "working"


def approve() -> None:
    gid = S.gate_id
    if S.stage == "gate_spec":
        S.events.append({"type": "user", "text": f"APPROVE SPEC {gid}", "command": True})
        close_gate("✅ Approved by you — installed as the contract for orders.")
        S.queue.append(turn(step("supervisor", text=f"Contract `{gid}` is installed for **orders**. Drafting the ETL now."),
                            rail={"Spec": "done"}, working="Supervisor is installing the contract…"))
        S.queue += script_turns()
        return
    S.events.append({"type": "user", "text": f"APPROVE {gid}", "command": True})
    close_gate("✅ Approved by you — ran once.")
    remembered = [p for p in (re.sub(r"`", "", c) for c in S.confirm) if p not in S.prefs]
    S.prefs += remembered
    S.queue += [
        turn(step("supervisor", text="Approved — starting the run." + (
            f" By approving you also confirmed {len(remembered)} point(s); they're remembered for orders." if remembered else "")),
             rail={"Approval": "done", "Run": "active"}, working="Supervisor is recording your approval…"),
        turn(step("execution", "Supervisor → Execution", text=f"Glue job `etl-copilot-job` **SUCCEEDED** in 20 s (run `{data.RUN_ID}`)."),
             rail={"Run": "done", "Validate": "active"}, working="Execution is running the Glue job…"),
        turn(step("execution", "Supervisor → Execution", text="Validated the output against the contract — **7/7 passed**.\n\n"
                  + data.RULES + "\n\n" + data.COUNTS),
             rail={"Validate": "done", "Document": "active"}, working="Execution is validating the output with Athena…"),
        turn(step("documenter", "Supervisor → Documenter", text="Wrote the docs (every figure computed from the table):",
                  bullets=data.DOCS),
             rail={"Document": "done"}, working="Documenter is writing the data dictionary…"),
        turn(step("supervisor", text="**orders is clean.**", bullets=data.FINAL), stage="done",
             working="Supervisor is wrapping up…"),
    ]


def reject() -> None:
    gid, kind = S.gate_id, S.stage
    S.events.append({"type": "user", "text": f"REJECT {'SPEC ' if kind == 'gate_spec' else ''}{gid}", "command": True})
    close_gate("✕ Rejected by you — nothing ran.")
    S.queue.append(turn(step("supervisor", text="Rejected — **nothing ran** and nothing was installed. "
                                                "Tell me what to change, or upload another file."),
                        rail={"Approval" if kind == "gate_draft" else "Spec": "failed"}, stage="rejected",
                        working="Supervisor is stopping…"))


def apply_note(note: str, as_command: bool = False) -> None:
    """A preference sent by chat, a suggestion chip or 'Request changes'."""
    gid = S.gate_id
    spec = S.stage == "gate_spec"
    S.events.append({"type": "user", "text": f"REJECT {'SPEC ' if spec else ''}{gid} {note}" if as_command else note,
                     "command": as_command})
    if spec:  # the Spec Writer revises the proposal; a new contract id needs a new approval
        new_id = hashlib.sha256((gid + note).encode()).hexdigest()[:12]
        summary = data.SPEC_PROPOSAL + [f"{note} (your note)"]
        close_gate(f"↺ Superseded by contract {new_id} after your note.")
        S.queue += [turn(step("spec", "Supervisor → Spec Writer", text=f"Got it — revised the contract: *{note}*.",
                              diff=[("+", f"{note} (your note)")]), working="Spec Writer is revising the contract…"),
                    turn({"type": "gate", "kind": "spec", "id": new_id, "title": "Approve the contract first",
                          "summary": summary, "guardrails": ["nothing runs yet"], "code": data.spec_json()},
                         rail={"Spec": "waiting"}, stage="gate_spec", working="Supervisor is preparing the approval…",
                         then=("gate", new_id))]
        return
    if S.stage != "gate_draft":
        S.queue.append(turn(step("supervisor", text="Noted. In the live app I route this to the right agent; "
                                                    "the mock only scripts notes while a draft awaits approval."),
                            working="Supervisor is reading your message…"))
        return
    effect = next((v for k, v in data.NOTE_EFFECTS.items() if k in note.lower()), "generic")
    if effect is None:  # already true: say so, change nothing, keep the same gate
        S.queue.append(turn(step("supervisor", "Supervisor → Script Writer",
                                 text="That's already how the draft works (slash dates are parsed as DD/MM). "
                                      "**Nothing changed** — the draft awaiting approval is the same."),
                            working="Script Writer is checking the draft…"))
        return
    old = list(S.plan)
    if effect == "generic":
        S.plan.append(f"{note} (your note)")
        ack = f"Got it — applying: *{note}*."
    else:
        before, after, ack = effect
        S.plan = [after if line == before else line for line in S.plan]
    changes = [(l[0], l[2:]) for l in difflib.ndiff(old, S.plan) if l[:1] in "+-"]
    new_id = hashlib.sha256((gid + note).encode()).hexdigest()[:12]
    close_gate(f"↺ Superseded by draft {new_id} after your note.")
    S.rememberable = note
    S.queue += [turn(step("script", "Supervisor → Script Writer", text=ack + " Here's what changed:", diff=changes),
                     rail={"Approval": "pending", "Draft": "active"}, working="Script Writer is revising the draft…"),
                draft_gate(new_id)]


def chip_clicked() -> None:
    note = S.get("chips")
    if note:
        apply_note(note)
    S.chips = None


def remember() -> None:
    """REMEMBER orders: <note> - only on the engineer's click."""
    if S.rememberable not in S.prefs:
        S.prefs.append(S.rememberable)
    S.rememberable = None


def forget(i: int) -> None:
    S.prefs.pop(i)


# ---- sidebar ---------------------------------------------------------------------------
with st.sidebar:
    view.rail(S.rail)
    st.divider()
    view.preference_chips(S.prefs, on_forget=forget)
    st.divider()
    st.html("<div class='cc-section'>Mock controls</div>")
    st.radio("Dataset", ["known", "new"], key="scenario", disabled=S.stage != "upload",
             format_func=lambda v: "Known dataset — one approval" if v == "known" else "New dataset — contract, then script")
    st.toggle("Fast playback", key="fast")
    st.button("Start over", on_click=reset, icon=":material/restart_alt:")

# ---- main area -------------------------------------------------------------------------
view.header(S.file, S.stage, badge="mock")

if S.stage == "upload":
    st.markdown("### Drop a CSV — that's it")
    st.caption("ETL Copilot profiles it, proposes the cleaning, and waits for your approval before anything runs. "
               "*Mock: the narration below is the real orders onboarding, whatever file you pick.*")
    up = st.file_uploader("CSV file", type=["csv"], label_visibility="collapsed")
    if up is not None:
        try:
            rows = sum(1 for _ in csv.reader(io.StringIO(up.getvalue().decode("utf-8-sig")))) - 1
            st.button(f"Onboard {up.name}", type="primary", on_click=start, args=(up.name, rows))
        except UnicodeDecodeError:
            st.error("That file isn't a text CSV — nothing was uploaded.")
    st.button("Use the sample: orders_export.csv (508 rows)", on_click=start, args=("orders_export.csv", 508),
              icon=":material/description:")

for ev in S.events:
    if ev["type"] == "gate":
        view.gate_card(ev, active=ev["id"] == S.gate_id, closed_note=S.closed.get(ev["id"], ""),
                       on_approve=approve, on_reject=reject, on_request=lambda r: apply_note(r, as_command=True))
    else:
        view.message(ev)

if S.rememberable and S.stage == "gate_draft":
    st.button(f"Remember for orders: “{S.rememberable}”", icon=":material/bookmark_add:", on_click=remember)

busy = bool(S.queue)
if busy:
    nxt = S.queue[0]
    agent = nxt["event"].get("agent", "supervisor") if nxt["event"]["type"] == "step" else "supervisor"
    with st.chat_message(view.AGENTS[agent][0], avatar=view.avatar(agent)):
        st.caption(f"⏳ {nxt['working']}")

if S.stage in ("gate_draft", "gate_spec") and not busy:
    st.pills("Suggestions", data.SUGGESTIONS, key="chips", on_change=chip_clicked, label_visibility="collapsed")

note = st.chat_input("Working…" if busy else 'Type a preference… e.g. "use DD/MM for dates"',
                     disabled=busy or S.stage == "upload")
if note:
    apply_note(note)
    st.rerun()

# ---- playback: one scripted turn per rerun ------------------------------------------------
if busy:
    time.sleep(0.25 if S.get("fast") else 1.1)
    t = S.queue.pop(0)
    S.events.append(t["event"])
    S.rail.update(t["rail"])
    if t["stage"]:
        S.stage = t["stage"]
    if t["then"]:
        S.gate_id = t["then"][1]
    st.rerun()
