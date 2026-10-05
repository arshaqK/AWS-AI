"""Copilot Console - the ETL Copilot UI (Phase 5), on localhost.

Upload one CSV; the agents run the pipeline and narrate each step; you decide at the
approval card. Run from etl-copilot/ui (with `agentcore dev --port 8081 --skip-deploy`
running for the Local backend):
    uv run streamlit run app.py
"""
import re
import sys
from functools import partial

import boto3
import streamlit as st

import backend
import console_view as view
import upload

backend.load_local_env()
sys.path.insert(0, str(backend.ROOT / "app" / "EtlCopilot"))
import memory  # noqa: E402  (read-only here: conventions for the sidebar)

st.set_page_config(page_title="ETL Copilot Console", page_icon="🧹", layout="centered")
view.inject_theme()
S = st.session_state

BACKENDS = {"local": backend.LocalBackend(), "deployed": backend.DeployedBackend()}
RAIL_BY_PHASE = {
    "profile": {"Profile": "done", "Spec": "active"},
    "spec": {"Spec": "done", "Draft": "active"},
    "draft": {"Draft": "done"},
    "run_started": {"Approval": "done", "Run": "active"},
    "docs": {"Document": "done"},
}
view.STATUS_PILLS.update({"ready": ("Ready", "idle"), "failed": ("Validation failed", "bad"),
                          "error": ("Error", "bad")})


def new_session() -> None:
    keep = {k: S[k] for k in ("backend_choice", "fake_backend", "s3_override") if k in S}
    S.clear()
    S.update(keep)


S.setdefault("session_id", backend.new_session_id())
S.setdefault("feed", [])          # user messages, narration events, errors - in order
S.setdefault("turn", None)        # the turn in flight, if any
S.setdefault("seen", 0)           # how many of the turn's events are already in the feed
S.setdefault("turn_has_gate", False)
S.setdefault("rail", {})
S.setdefault("stage", "upload")
S.setdefault("file", "")
S.setdefault("dataset", "")
S.setdefault("gate_id", None)
S.setdefault("decided", {})       # gate id -> how it was closed ("✅ Approved by you", superseded...)
S.setdefault("plain_reject", False)
S.setdefault("backend_choice", "local")

# The engineer's exact commands - the same ones approvals.py accepts on the agent side.
COMMAND_RE = re.compile(r"^\s*(APPROVE|REJECT)\s+(SPEC\s+)?([0-9a-f]{12})\b\s*(.*)$", re.IGNORECASE | re.DOTALL)


def current_backend():
    return S.get("fake_backend") or BACKENDS[S.backend_choice]


@st.cache_resource(show_spinner=False)
def _s3_client():
    return boto3.client("s3", region_name=backend.REGION)  # one client: resolving the login is slow


def s3():
    return S.get("s3_override") or _s3_client()


def upload_and_onboard(name: str, data: bytes, dataset: str, rows: int) -> None:
    """Button callback: runs before the page redraws, so the next frame already shows the feed."""
    try:
        uri = upload.upload(s3(), dataset, name, data)
    except Exception as e:
        S.upload_error = str(e)
        return
    S.file, S.dataset = name, dataset
    send(f"Onboard {dataset}", shown=f"📎 Uploaded **{name}** · {rows} rows → `{uri}`")


def fetch(uri: str) -> bytes:
    """An s3:// file's bytes, read with your own login (for the Download button)."""
    bucket, key = uri[len("s3://"):].split("/", 1)
    return s3().get_object(Bucket=bucket, Key=key)["Body"].read()


def send(prompt: str, shown: str = None) -> None:
    """Show the engineer's message and start the turn in the background."""
    command = COMMAND_RE.match(prompt)
    S.plain_reject = False
    if command:  # typed or clicked, a decision closes its card
        verb, gid, reason = command.group(1).upper(), command.group(3).lower(), command.group(4).strip()
        S.decided[gid] = ("✅ Approved by you" if verb == "APPROVE" else
                          f"↺ Changes requested: {reason}" if reason else "✕ Rejected by you — nothing ran")
        S.plain_reject = verb == "REJECT" and not reason
        if verb == "APPROVE" and command.group(2):  # the contract is installed by this very message
            S.rail.update({"Spec": "done", "Draft": "active"})
    S.feed.append({"type": "user", "text": shown or prompt, "command": bool(command)})
    S.turn, S.seen, S.turn_has_gate = backend.start_turn(current_backend(), prompt, S.session_id), 0, False
    S.stage = "working"


def decide(item: dict, verb: str, reason: str = "") -> None:
    """A card button: send exactly the command the engineer would type."""
    send(f"{verb} {'SPEC ' if item['kind'] == 'spec' else ''}{item['id']}" + (f" {reason.strip()}" if reason.strip() else ""))


def set_rail(updates: dict, force: bool = False) -> None:
    """A finished step never goes back to active or pending, except when a new draft resets
    the run steps (force) - events from different agents can arrive in any order."""
    for step, state in updates.items():
        if force or S.rail.get(step) != "done" or state in ("done", "failed", "waiting"):
            S.rail[step] = state


def take_events() -> None:
    """Move what the worker thread received into the feed, updating the rail and the stage."""
    turn = S.turn
    if turn is None:
        return
    for ev in turn.events[S.seen:]:
        S.seen += 1
        phase = ev.get("phase")
        if ev["type"] == "gate":
            if S.gate_id and S.gate_id != ev["id"] and S.gate_id not in S.decided:
                S.decided[S.gate_id] = f"↺ Superseded by {'contract' if ev['kind'] == 'spec' else 'draft'} {ev['id']}"
            if ev["kind"] == "spec":
                set_rail({"Spec": "waiting"})
            else:  # a new or revised draft: anything that failed before is pending again
                set_rail({"Draft": "done", "Approval": "waiting", "Run": "pending", "Validate": "pending",
                          "Document": "pending"}, force=True)
            S.gate_id, S.turn_has_gate = ev["id"], True
        elif phase == "run":
            set_rail({"Run": "done", "Validate": "active"} if ev.get("state") == "SUCCEEDED" else {"Run": "failed"})
        elif phase == "validate":
            set_rail({"Validate": "done", "Document": "active"} if ev.get("passed") else {"Validate": "failed"})
        elif phase in RAIL_BY_PHASE:
            set_rail(RAIL_BY_PHASE[phase])
        if ev["type"] == "final":
            ev = closing_message(turn, ev)
            if ev is None:
                continue
        S.feed.append(ev)
    if turn.done and S.seen >= len(turn.events):
        if turn.error:
            S.feed.append({"type": "error", "text": turn.error})
        S.stage = closing_stage(turn)
        S.turn = None


def closing_message(turn, final: dict):
    """What to show for the Supervisor's closing reply, without repeating the cards above it.

    A turn that ends at a card: nothing (the card is the summary). A passing run: one result
    card. Anything else (a question, a refusal, a list): the reply itself.
    """
    if S.turn_has_gate:
        return None
    docs = [e for e in turn.events if e.get("phase") == "docs"]
    checks = [e for e in turn.events if e.get("phase") == "validate" and e.get("passed")]
    if docs and checks:
        v = checks[-1]
        return {"type": "result", "dataset": v.get("dataset") or S.dataset, "rules_passed": v.get("rules_passed"),
                "rules_total": v.get("rules_total"), "attempt": v.get("attempt"), "files": docs[-1].get("files", {})}
    return final


def closing_stage(turn) -> str:
    if turn.error:
        return "error"
    if S.plain_reject and not any(e["type"] == "gate" for e in turn.events):
        S.rail.update({"Approval" if S.rail.get("Approval") == "waiting" else "Spec": "failed"})
        return "rejected"
    gates = [e for e in turn.events if e["type"] == "gate"]
    if gates:
        return "gate_spec" if gates[-1]["kind"] == "spec" else "gate_draft"
    if any(e.get("phase") == "docs" for e in turn.events):
        return "done"
    if any(e.get("phase") == "validate" and not e.get("passed") for e in turn.events):
        return "failed"
    return "ready"


@st.cache_data(ttl=10, show_spinner=False)
def backend_ok(choice: str) -> bool:
    return BACKENDS[choice].healthy()


@st.cache_data(ttl=120, show_spinner=False)
def conventions(dataset: str) -> list:
    try:
        return memory.conventions(dataset) if dataset and memory.enabled() else []
    except Exception:
        return []


def sidebar_conventions(busy: bool) -> list:
    """Looked up only while idle (a first lookup takes seconds); during a turn the last list is shown."""
    if not busy:
        S.prefs_shown = (S.dataset, conventions(S.dataset))
    shown_for, prefs = S.get("prefs_shown", ("", []))
    return prefs if shown_for == S.dataset else []


take_events()
busy = S.turn is not None

# ---- sidebar ---------------------------------------------------------------------------
with st.sidebar:
    view.rail(S.rail)
    st.divider()
    st.html("<div class='cc-section'>Active preferences</div>")
    prefs = sidebar_conventions(busy)
    if not S.dataset:
        st.caption("Shown once a dataset is chosen.")
    elif not prefs:
        st.caption(f"Nothing remembered for {S.dataset} yet.")
    for p in prefs:
        st.html(f"<span class='cc-chip'>{view.escape(p)}</span>")
    st.divider()
    st.html("<div class='cc-section'>Backend</div>")
    st.radio("Backend", list(BACKENDS), key="backend_choice", disabled=busy, label_visibility="collapsed",
             format_func=lambda k: BACKENDS[k].name)
    if "fake_backend" not in S and not busy:  # a busy agent answers pings slowly: check while idle
        ok = backend_ok(S.backend_choice)
        st.caption(("🟢 reachable" if ok else "🔴 not reachable")
                   + (" — start `agentcore dev --port 8081 --skip-deploy`" if not ok and S.backend_choice == "local" else ""))
    st.caption(f"Session `{S.session_id[-12:]}`")
    st.button("New session", on_click=new_session, icon=":material/restart_alt:", disabled=busy)

# ---- main area -------------------------------------------------------------------------
view.header(S.file, S.stage)

if S.stage == "upload":
    st.markdown("### Drop a CSV — that's it")
    st.caption("ETL Copilot profiles it, proposes the cleaning, and waits for your approval before anything runs.")
    picked = st.file_uploader("CSV file", type=["csv"], label_visibility="collapsed")
    test_file = S.get("test_upload")  # (name, bytes) - lets the headless tests skip the browser picker
    if picked is not None or test_file:
        name, data = test_file or (picked.name, picked.getvalue())
        try:
            info = upload.check_csv(name, data)
            st.success(f"**{name}** · {info['rows']} rows · {len(info['columns'])} columns")
            dataset = st.text_input("Dataset name", value=upload.dataset_from_filename(name),
                                    help="Its folder under landing/ and the name of the clean table.")
            upload.check_dataset(dataset)
            there = upload.existing_files(s3(), dataset)
            others = [f for f in there if f != name]
            agreed = True
            if name in there:
                st.info(f"landing/{dataset}/{name} already exists — it will be replaced.")
            if others:
                st.warning(f"landing/{dataset}/ also holds {', '.join(others)}. Every file in the folder "
                           "is read as one table.")
                agreed = st.checkbox("Upload it next to them anyway", key="agree_others")
            reachable = "fake_backend" in S or backend_ok(S.backend_choice)
            if not reachable:
                st.caption("The agent isn't reachable — start it (see the sidebar) before uploading.")
            st.button(f"Upload & onboard {dataset}", type="primary", disabled=not (agreed and reachable),
                      icon=":material/upload:", on_click=upload_and_onboard, args=(name, data, dataset, info["rows"]))
            if S.get("upload_error"):
                st.error(f"The upload failed: {S.pop('upload_error')}")
        except upload.UploadError as e:
            st.error(str(e))

for item in S.feed:
    if item["type"] == "gate":
        live = (item["id"] == S.gate_id and item["id"] not in S.decided and not busy
                and S.stage in ("gate_spec", "gate_draft"))
        view.gate_card(item, active=live, closed_note=S.decided.get(item["id"], ""),
                       on_approve=partial(decide, item, "APPROVE"), on_reject=partial(decide, item, "REJECT"),
                       on_request=partial(decide, item, "REJECT"))
    elif item["type"] == "result":
        csv_uri = item["files"].get("clean_csv")
        view.result_card(item["dataset"], item["rules_passed"], item["rules_total"], item["attempt"], item["files"],
                         download=partial(fetch, csv_uri) if csv_uri else None)
    elif item["type"] == "final":
        view.message({"type": "step", "agent": "supervisor", "text": item["text"]})
    elif item["type"] == "error":
        with st.chat_message("Supervisor", avatar=view.avatar("supervisor")):
            st.error(f"The agent stopped with an error: {item['text']}")
    else:
        view.message(item)


@st.fragment(run_every=1.0)
def live_status() -> None:
    """While a turn runs, only this line refreshes (no page-wide flicker); the page itself
    reruns once, to completion, when the turn has something new for the feed or has ended."""
    turn = S.turn
    if turn is None:
        return
    if len(turn.events) > S.seen or turn.done:
        st.rerun(scope="app")
    agent = turn.activity_agent if turn.activity_agent in view.AGENTS else "supervisor"
    with st.chat_message(view.AGENTS[agent][0], avatar=view.avatar(agent)):
        st.html(f"<div class='cc-activity'><span class='cc-spin'></span>{view.escape(turn.activity)}… "
                f"<span class='cc-elapsed'>{turn.elapsed}s</span></div>")


if busy:
    live_status()

note = st.chat_input("ETL Copilot is working…" if busy else
                     "Message ETL Copilot… e.g. \"keep customer_name, it's a company name\"",
                     disabled=busy or S.stage == "upload")
if note:
    send(note.strip())
    st.rerun()
