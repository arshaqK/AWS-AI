"""How the Copilot Console looks: theme, header, status rail, agent messages, approval card.

Rendering only - no AWS and no state machine. Everything is drawn from plain event dicts
in the shape the agent will stream in step 5.1, so the mock (mock_app.py) and the real
app render through the same code:

    {"type": "user", "text": ...}
    {"type": "step", "agent": "profile", "route": "Supervisor → Profile",
     "bullets": [...], "text": ..., "code": ..., "diff": [(sign, line), ...]}
    {"type": "gate", "kind": "spec" | "draft", "id": ..., "title": ..., "summary": [...],
     "needs_confirmation": [...], "guardrails": [...], "code": ...}
"""
import re
from html import escape
from urllib.parse import quote

import streamlit as st

# name, avatar letter, colour (the runbook's --ag-* tokens)
AGENTS = {
    "user": ("You", "U", "#9db2bf"),
    "supervisor": ("Supervisor", "S", "#4dbecb"),
    "profile": ("Profile", "P", "#84a9e0"),
    "spec": ("Spec Writer", "C", "#63c395"),
    "script": ("Script Writer", "W", "#e0c065"),
    "execution": ("Execution", "E", "#e08a7d"),
    "documenter": ("Documenter", "D", "#b79ae0"),
}
STEPS = ["Profile", "Spec", "Draft", "Approval", "Run", "Validate", "Document"]
STATUS_PILLS = {  # stage -> (label, css variant)
    "upload": ("Waiting for a file", "idle"),
    "working": ("Working", "work"),
    "gate_spec": ("Awaiting approval", "wait"),
    "gate_draft": ("Awaiting approval", "wait"),
    "done": ("Passed", "ok"),
    "rejected": ("Rejected", "bad"),
}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@600;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
:root { --bg:#0e181f; --card:#16242e; --card2:#1a2b36;
  --ink:#e6eef3; --muted:#9db2bf; --faint:#728896; --line:#2a3d49; --chip:#1d2f3a;
  --accent:#4dbecb; --accent-bg:#12343a; --accent-ink:#7ad4de;
  --gate:#63c395; --gate-bg:#123026; --gate-ink:#8fdcb4; --gate-line:#2f6b52;
  --guard-bg:#332a12; --guard-ink:#e6cf83; --guard-line:#6b5626;
  --danger:#e08a7d; --danger-bg:#3a201c;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,monospace; }
.stApp, .stApp p, .stApp li, .stApp label, .stApp input, .stApp textarea { font-family:"IBM Plex Sans",system-ui,sans-serif; }
.stApp code { font-family:var(--mono); }
.block-container { max-width: 980px; padding-top: 4.5rem; }

.cc-header { display:flex; align-items:center; gap:10px; background:var(--card); border:1px solid var(--line);
  border-radius:12px; padding:10px 16px; }
.cc-logo { width:26px; height:26px; border-radius:7px; background:var(--accent); color:var(--bg); display:grid;
  place-items:center; font:700 14px "Bricolage Grotesque",sans-serif; }
.cc-title { font:700 17px "Bricolage Grotesque",sans-serif; color:var(--ink); }
.cc-file { font:13px var(--mono); color:var(--muted); }
.cc-badge { font:11px var(--mono); color:var(--muted); background:var(--chip); border-radius:6px; padding:2px 6px; }
.cc-pill { margin-left:auto; border-radius:999px; padding:3px 11px; font-size:12px; font-weight:600; white-space:nowrap; }
.cc-pill.idle { background:var(--chip); color:var(--muted); }
.cc-pill.work { background:var(--accent-bg); color:var(--accent-ink); }
.cc-pill.wait { background:var(--guard-bg); color:var(--guard-ink); border:1px solid var(--guard-line); }
.cc-pill.ok { background:var(--gate-bg); color:var(--gate-ink); border:1px solid var(--gate-line); }
.cc-pill.bad { background:var(--danger-bg); color:var(--danger); }

.cc-section { font-size:11px; font-weight:600; letter-spacing:.06em; text-transform:uppercase; color:var(--faint);
  margin:4px 0 8px; }
.cc-rail { list-style:none; padding:0; margin:0 0 6px; }
.cc-rail li { display:flex; align-items:center; gap:10px; padding:5px 0; font-size:14px; color:var(--faint); }
.cc-rail .dot { width:18px; height:18px; border-radius:50%; border:2px solid var(--line); display:grid;
  place-items:center; font-size:11px; font-weight:700; background:var(--card); }
.cc-rail li.done { color:var(--ink); } .cc-rail li.done .dot { background:var(--gate); border-color:var(--gate); color:var(--bg); }
.cc-rail li.active { color:var(--accent-ink); font-weight:600; }
.cc-rail li.active .dot { border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-bg); }
.cc-rail li.waiting { color:var(--guard-ink); font-weight:600; }
.cc-rail li.waiting .dot { border-color:var(--guard-ink); background:var(--guard-bg); }
.cc-rail li.failed { color:var(--danger); } .cc-rail li.failed .dot { background:var(--danger); border-color:var(--danger); color:var(--bg); }

.cc-who { font-size:13px; font-weight:600; margin-bottom:2px; }
.cc-route { display:inline-block; font:11px var(--mono); color:var(--accent-ink); background:var(--accent-bg);
  border-radius:999px; padding:1px 9px; margin-top:6px; }
.cc-cmd { font:13px var(--mono); background:var(--chip); color:var(--ink); border:1px solid var(--line); border-radius:6px; padding:2px 8px; }
.cc-diff { font:12.5px var(--mono); border:1px solid var(--line); border-radius:8px; overflow:hidden; margin:6px 0; }
.cc-diff div { padding:3px 10px; }
.cc-diff .del { background:var(--danger-bg); color:var(--danger); }
.cc-diff .add { background:var(--gate-bg); color:var(--gate-ink); }
.cc-guard { background:var(--guard-bg); border:1px solid var(--guard-line); color:var(--guard-ink);
  border-radius:8px; padding:7px 11px; font-size:13px; margin:6px 0; }
.cc-confirm { border-left:3px solid var(--guard-ink); background:var(--guard-bg); color:var(--guard-ink); padding:6px 11px; margin:6px 0;
  font-size:13.5px; border-radius:0 8px 8px 0; }
.cc-gate-title { font:700 16px "Bricolage Grotesque",sans-serif; color:var(--gate-ink); }
.cc-gate-id { font:12px var(--mono); color:var(--muted); }
.cc-closed { font-size:12.5px; color:var(--muted); font-style:italic; }
.cc-chip { display:inline-block; background:var(--chip); color:var(--ink); border:1px solid var(--line); border-radius:12px;
  padding:3px 10px; font-size:12.5px; margin:2px 0; }
[class*="st-key-gate-"] { background:var(--card); border-color:var(--gate-line) !important; }
/* No page-wide pulsing while the agent works: keep elements solid during reruns and hide
   Streamlit's running indicator - the status line below the feed says what is happening. */
.stApp [data-stale="true"], .stApp .stale-element { opacity:1 !important; transition:none !important; }
[data-testid="stStatusWidget"] { visibility:hidden; }
.cc-activity { display:flex; align-items:center; gap:10px; font-size:14px; color:var(--accent-ink); }
.cc-elapsed { font:12px var(--mono); color:var(--faint); }
.cc-spin { width:12px; height:12px; border-radius:50%; border:2px solid var(--line);
  border-top-color:var(--accent); animation:cc-spin .9s linear infinite; flex:none; }
@keyframes cc-spin { to { transform:rotate(360deg); } }

/* Theme-proof: every surface dark, every text light - whatever Streamlit theme the browser cached */
.stApp, [data-testid="stHeader"], [data-testid="stBottom"], [data-testid="stBottom"] > div,
[data-testid="stBottomBlockContainer"] { background:var(--bg) !important; }
[data-testid="stSidebar"], [data-testid="stSidebarContent"] { background:var(--card) !important; }
.stApp, .stApp p, .stApp li, .stApp label, .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp strong, .stApp em,
.stApp td, .stApp th, [data-testid="stMarkdownContainer"], [data-testid="stWidgetLabel"] p { color:var(--ink); }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { color:var(--muted) !important; }
[data-testid="stIconMaterial"] { color:var(--muted); }
.stApp :not(pre) > code { background:var(--chip) !important; color:var(--accent-ink) !important;
  padding:1px 5px; border-radius:4px; }
.stApp pre, [data-testid="stCode"] pre { background:var(--card2) !important; border:1px solid var(--line); }
.stApp pre code, .stApp pre code span { color:var(--ink) !important; background:transparent !important; }
.stApp pre .token.keyword, .stApp pre .token.boolean { color:#4dbecb !important; }
.stApp pre .token.function, .stApp pre .token.class-name { color:#84a9e0 !important; }
.stApp pre .token.string, .stApp pre .token.triple-quoted-string { color:#63c395 !important; }
.stApp pre .token.number { color:#e0c065 !important; }
.stApp pre .token.builtin, .stApp pre .token.property { color:#b79ae0 !important; }
.stApp pre .token.comment { color:#728896 !important; font-style:italic; }
.stApp pre .token.punctuation, .stApp pre .token.operator { color:#9db2bf !important; }
[data-testid="stExpander"] details, [data-testid="stExpander"] summary, [data-testid="stExpanderDetails"] {
  background:var(--card) !important; color:var(--ink) !important; border-color:var(--line) !important; }
.stApp table { border-collapse:collapse; }
.stApp th { background:var(--card2) !important; }
.stApp th, .stApp td { border:1px solid var(--line) !important; }
[data-testid="stChatInput"], [data-testid="stChatInput"] > div { background:var(--card) !important;
  border-color:var(--line) !important; }
.stApp textarea, .stApp input { background:var(--card) !important; color:var(--ink) !important;
  caret-color:var(--accent); }
.stApp textarea::placeholder, .stApp input::placeholder { color:var(--faint) !important; }
[data-testid="stTextInputRootElement"], [data-baseweb="input"], [data-baseweb="base-input"] {
  background:var(--card) !important; border-color:var(--line) !important; }
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-pills"], [data-testid="stBaseButton-pillsActive"] {
  background:var(--card2) !important; color:var(--ink) !important; border:1px solid var(--line) !important; }
[data-testid="stBaseButton-secondary"] p, [data-testid="stBaseButton-pills"] p { color:var(--ink) !important; }
[data-testid="stBaseButton-secondary"]:hover, [data-testid="stBaseButton-pills"]:hover { border-color:var(--accent) !important; }
[data-testid="stBaseButton-primary"] { background:var(--accent) !important; border:none !important; }
[data-testid="stBaseButton-primary"] p { color:var(--bg) !important; font-weight:600; }
[data-testid="stFileUploaderDropzone"] { background:var(--card) !important; border:1px dashed var(--line) !important; }
[data-testid="stFileUploaderDropzone"] span, [data-testid="stFileUploaderDropzone"] small { color:var(--ink) !important; }
[data-testid="stChatMessage"] [data-testid="stVerticalBlockBorderWrapper"] { background:var(--card) !important; }
.stApp button[data-variant="pills"] { background:var(--card2) !important; border:1px solid var(--line) !important; }
.stApp button[data-variant="pills"] p { color:var(--ink) !important; }
.stApp button[data-variant="pills"]:hover { border-color:var(--accent) !important; }
.stApp button[data-variant="pills"]:hover p { color:var(--accent-ink) !important; }
[data-testid="stBaseButton-elementToolbar"] { background:var(--chip) !important; color:var(--ink) !important; }
[data-testid="stRadioOption"] p { color:var(--ink) !important; }
[data-testid="stRadioOption"][data-selected="false"] > div:first-of-type { background:var(--card2) !important;
  border:2px solid var(--faint) !important; }
.stApp .cc-diff code { background:transparent !important; color:inherit !important; padding:0; }
[data-testid="stHeader"] { border-bottom:1px solid var(--line); }
[data-testid="stSidebar"] { background:var(--card); border-right:1px solid var(--line); }
[data-testid="stChatMessage"] { background:transparent; }
.stApp button[kind="primary"] { color:var(--bg); font-weight:600; }
.stApp [data-testid="stMarkdownContainer"] table { border-color:var(--line); }
</style>
"""


def inline(text: str) -> str:
    """Agent text as safe HTML: escaped, with `code` spans kept."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(text))


def avatar(agent: str) -> str:
    """A filled circle with the agent's letter, as an inline SVG (the mockup's avatars)."""
    _, letter, colour = AGENTS[agent]
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='64' height='64'><circle cx='32' cy='32' r='32' "
           f"fill='{colour}'/><text x='32' y='42' font-family='Helvetica,Arial,sans-serif' font-size='28' "
           f"font-weight='700' fill='#0e181f' text-anchor='middle'>{letter}</text></svg>")
    return "data:image/svg+xml;utf8," + quote(svg)


def inject_theme() -> None:
    st.html(CSS)


def header(file_name: str, stage: str, badge: str = "") -> None:
    label, variant = STATUS_PILLS.get(stage, STATUS_PILLS["working"])
    file_part = f"<span class='cc-file'>· {escape(file_name)}</span>" if file_name else ""
    badge_part = f"<span class='cc-badge'>{escape(badge)}</span>" if badge else ""
    st.html(f"<div class='cc-header'><div class='cc-logo'>E</div><span class='cc-title'>etl-copilot</span>"
            f"{file_part}{badge_part}<span class='cc-pill {variant}'>{label}</span></div>")


def rail(states: dict) -> None:
    """Run status: each step is pending, active, waiting (for you), done or failed."""
    marks = {"done": "✓", "failed": "✕"}
    items = "".join(f"<li class='{states.get(s, 'pending')}'><span class='dot'>{marks.get(states.get(s), '')}</span>{s}</li>"
                    for s in STEPS)
    st.html(f"<div class='cc-section'>Run status</div><ul class='cc-rail'>{items}</ul>")


def diff_html(lines) -> str:
    rows = "".join(f"<div class='{'del' if sign == '-' else 'add'}'>{'−' if sign == '-' else '+'} {inline(text)}</div>"
                   for sign, text in lines)
    return f"<div class='cc-diff'>{rows}</div>"


def message(event: dict) -> None:
    """One chat turn: a user message or an agent's step narration."""
    agent = "user" if event["type"] == "user" else event.get("agent", "supervisor")
    name = AGENTS[agent][0]
    with st.chat_message(name, avatar=avatar(agent)):
        if event["type"] == "user":
            if event.get("command"):
                st.html(f"<span class='cc-cmd'>{escape(event['text'])}</span>")
            else:
                st.markdown(event["text"])
            return
        st.html(f"<div class='cc-who' style='color:{AGENTS[agent][2]}'>{name}</div>")
        if event.get("text"):
            st.markdown(event["text"])
        if event.get("bullets"):
            st.markdown("\n".join(f"- {b}" for b in event["bullets"]))
        if event.get("diff"):
            st.html(diff_html(event["diff"]))
        if event.get("code"):
            with st.expander(event.get("code_label", "Show generated script")):
                st.code(event["code"], language="python")
        if event.get("route"):
            st.html(f"<span class='cc-route'>{escape(event['route'])}</span>")


def gate_card(event: dict, active: bool, closed_note: str = "", on_approve=None, on_reject=None,
              on_request=None) -> None:
    """The single place a human decides. Inactive cards stay in the feed, read-only."""
    with st.chat_message("Supervisor", avatar=avatar("supervisor")):
        with st.container(border=True, key=f"gate-{event['kind']}-{event['id']}"):
            what = "contract" if event["kind"] == "spec" else "ETL script"
            st.html(f"<div class='cc-gate-title'>{escape(event.get('title', 'Ready for your approval'))}</div>"
                    f"<div class='cc-gate-id'>{what} · {event['kind']} id {escape(event['id'])}</div>")
            heading = "The contract" if event["kind"] == "spec" else "The plan"
            st.markdown(f"**{heading}**\n" + "\n".join(f"- {s}" for s in event["summary"]))
            if event.get("changed"):
                st.markdown("**What changed**\n" + "\n".join(f"- {c}" for c in event["changed"]))
            points = event.get("needs_confirmation") or []
            if points:
                st.markdown("**Check before approving** — approving confirms "
                            + ("this guess" if len(points) == 1 else f"these {len(points)} guesses")
                            + " and remembers it for this dataset:")
                for p in points:
                    st.html(f"<div class='cc-confirm'>{inline(p)}</div>")
            if event.get("guardrails"):
                st.html("<div class='cc-guard'><b>Guardrails:</b> " + " · ".join(inline(g) for g in event["guardrails"]) + "</div>")
            if event.get("code"):
                with st.expander("Show the full script" if event["kind"] == "draft" else "Show the contract"):
                    st.code(event["code"], language="python" if event["kind"] == "draft" else "json")
            if not active:
                st.html(f"<div class='cc-closed'>{escape(closed_note)}</div>")
                return
            approve_label = "Approve & run" if event["kind"] == "draft" else "Approve contract"
            a, r, x = st.columns([1.2, 1.3, 1])
            a.button(approve_label, type="primary", key=f"approve-{event['id']}", on_click=on_approve,
                     width="stretch")
            requesting = r.toggle("Request changes", key=f"request-open-{event['id']}")
            x.button("Reject", key=f"reject-{event['id']}", on_click=on_reject, width="stretch")
            if requesting:
                # A form submits exactly what is in the box when Send is pressed (a plain text
                # box can hand a button its value from before the last keystrokes).
                box = f"reason-{event['id']}"
                with st.form(key=f"request-form-{event['id']}", border=False):
                    st.text_area("What should change? List every change — they are all sent, word for word.",
                                 key=box, height=100,
                                 placeholder="e.g. keep customer_name, it's a company name, drop the notes column")
                    st.form_submit_button("Send to the agent", type="primary", on_click=_send_request,
                                          args=(on_request, box))


def _send_request(on_request, box: str) -> None:
    reason = (st.session_state.get(box) or "").strip()
    if reason and on_request:
        on_request(reason)


def result_card(dataset: str, rules_passed: int, rules_total: int, attempt, files: dict, download=None) -> None:
    """The end of a passing run, in one card (replaces the Supervisor's long summary).
    `download` is a callable returning the clean CSV's bytes, fetched only when clicked."""
    with st.chat_message("Supervisor", avatar=avatar("supervisor")):
        with st.container(border=True, key=f"result-{dataset}-{attempt}-{rules_passed}"):
            st.html(f"<div class='cc-gate-title'>✅ {escape(dataset)} is clean</div>"
                    f"<div class='cc-gate-id'>{rules_passed}/{rules_total} rules passed"
                    + (f" · attempt {attempt}" if attempt else "") + "</div>")
            lines = [f"- Table: `etl_copilot_clean.{dataset}` (Parquet in `staging/{dataset}/`)"]
            lines += [f"- {label}: `{files[key]}`" for key, label in
                      (("data_dictionary", "Data dictionary"), ("quality_report", "Quality report"),
                       ("clean_csv", "Clean data as CSV")) if key in files]
            st.markdown("\n".join(lines))
            if download and "clean_csv" in files:
                st.download_button(f"Download {dataset}.csv", data=download, file_name=f"{dataset}.csv",
                                   mime="text/csv", icon=":material/download:", on_click="ignore",
                                   key=f"download-{dataset}-{attempt}")


def preference_chips(prefs: list, on_forget) -> None:
    st.html("<div class='cc-section'>Active preferences</div>")
    if not prefs:
        st.caption("None yet. Approving a plan, or *Remember for this dataset*, adds them here.")
    for i, p in enumerate(prefs):
        chip, x = st.columns([6, 1], vertical_alignment="center")
        chip.html(f"<span class='cc-chip'>{escape(p)}</span>")
        x.button("", icon=":material/close:", key=f"forget-{i}", help="FORGET this convention", on_click=on_forget, args=(i,))
