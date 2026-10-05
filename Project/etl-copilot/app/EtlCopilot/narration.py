"""Narration for the Copilot Console (step 5.1): what each agent just did, as stream events.

Every event is built here by code from a real result - the profile dict, the saved spec,
the draft in S3, Glue's run record, the saved docs - never from what a model says it did.
Events reach the client through the entrypoint's stream as {"narration": {...}}:

    {"type": "step", "agent": "profile" | "spec" | "script" | "execution" | "documenter",
     "route": "Supervisor → Profile", "text": ..., "bullets": [...], "code": ...}
    {"type": "gate", "kind": "spec" | "draft", "id": ..., "dataset": ..., "summary": [...],
     "needs_confirmation": [...], "guardrails": [...], "code": ...}
    {"type": "final", "text": ...}
    {"type": "activity", "agent": ..., "text": "Script Writer is writing the ETL script for orders"}
      (what is happening right now - the console's status line, not part of the feed)

Tools run in worker threads (and sub-agents in threads of their own), so posting is
thread-safe. A tool called outside a request (tests, the CLI) posts nothing.
"""
import asyncio
import contextvars
import json
import logging
import re
import threading
from typing import Callable, List, Optional

progress = logging.getLogger("etl_copilot")

AGENT_NAMES = {"supervisor": "Supervisor", "profile": "Profile", "spec": "Spec Writer",
               "script": "Script Writer", "execution": "Execution", "documenter": "Documenter"}
MAX_VALUES_SHOWN = 4


class Channel:
    """One request's event queue, written from any thread, read by the entrypoint's loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, put: Callable[[dict], None]):
        self._loop, self._put = loop, put
        self.profiled: set = set()  # each dataset's profile is narrated once per request
        self.speaker = "supervisor"  # the specialist currently working, for "X → Profile"

    def post(self, event: dict) -> None:
        self._loop.call_soon_threadsafe(self._put, {"narration": event})


_current: contextvars.ContextVar = contextvars.ContextVar("narration_channel", default=None)
_latest: Optional[Channel] = None  # fallback for threads that did not inherit the context
_lock = threading.Lock()


def open_channel(loop, put) -> Channel:
    global _latest
    channel = Channel(loop, put)
    _current.set(channel)
    with _lock:
        _latest = channel
    return channel


def close_channel(channel: Channel) -> None:
    global _latest
    with _lock:
        if _latest is channel:
            _latest = None


def _channel() -> Optional[Channel]:
    return _current.get() or _latest


def post(event: Optional[dict]) -> None:
    """Send an event if a request is listening. Never raises: narration must not break a tool."""
    channel = _channel()
    if channel is None or not event:
        return
    try:
        channel.post(event)
    except Exception as e:
        progress.info("narration: could not post %s: %s", event.get("type"), e)


def activity(agent: str, text: str) -> None:
    """Say what is happening right now, in one short sentence."""
    post({"type": "activity", "agent": agent, "text": text})


def speaking(agent: str):
    """Mark which specialist is working, so a profile it asks for reads 'Script Writer → Profile'."""
    channel = _channel()
    if channel is not None:
        channel.speaker = agent


def route(to_agent: str) -> str:
    channel = _channel()
    by = channel.speaker if channel and channel.speaker != to_agent else "supervisor"
    return f"{AGENT_NAMES[by]} → {AGENT_NAMES[to_agent]}"


def first_profile(dataset: str) -> bool:
    """True the first time a dataset is profiled in this request (later calls stay quiet)."""
    channel = _channel()
    if channel is None or dataset in channel.profiled:
        return False
    channel.profiled.add(dataset)
    return True


# ---- builders: pure functions from real results to events --------------------------------
def _code(text) -> str:
    return f"`{str(text).replace('`', '')}`"


PERSONAL_NAME_HINTS = ("phone", "mobile", "email", "e_mail")
PERSON_HINTS = ("customer", "contact", "person", "first", "last", "full")
UNIT_SHAPE = re.compile(r"^[+-]?[9.,]+ ?a$")  # a number followed by one letter run: 9.9 kg, 999g


def _shape(shape: str) -> str:
    """Show a value shape with N for digits and x for letters: 9999-99-99 -> NNNN-NN-NN.
    A shape can then never read as an email or phone number."""
    return _code(shape.replace("9", "N").replace("a", "x").strip())


def _personal(column: dict) -> bool:
    name = column["name"].lower()
    shapes = [s["shape"] for s in column.get("top_shapes") or []]
    return (any("@" in s for s in shapes) or any(h in name for h in PERSONAL_NAME_HINTS)
            or ("name" in name and any(h in name for h in PERSON_HINTS)))


def _format_like(shape: str) -> bool:
    """Dates, times, numbers, prices: mostly digits, at most two letter runs (T and Z)."""
    return shape.count("9") >= 3 and shape.count("a") <= 2


def profile_step(profile: dict) -> dict:
    """Profile bullets from shapes, counts and low-cardinality values only. Personal-data
    columns get a label and nothing else, so no email, phone or name reaches the console."""
    cols = profile["columns"]
    bullets = [f"**{profile['row_count']} rows, {len(cols)} columns** (every row read via Athena)"]
    personal = []
    for c in cols:
        name = _code(c["name"])
        if _personal(c):
            personal.append(name)
            continue
        shapes = [s for s in c.get("top_shapes") or [] if s["shape"].strip()]
        units = UNIT_SHAPE.match(shapes[0]["shape"].strip()) if shapes else None
        if units and c.get("letter_tokens"):
            bullets.append(f"{name}: number + unit, units seen: " + ", ".join(_code(u) for u in list(c["letter_tokens"])[:6]))
        elif len(shapes) >= 2 and all(_format_like(s["shape"]) for s in shapes[:2]):
            shown = ", ".join(f"{_shape(s['shape'])} ({s['n']})" for s in shapes[:3])
            bullets.append(f"{name}: {len(shapes)} formats: {shown}")
        values = c.get("values") or {}
        if values and len({v.strip().lower() for v in values}) < len(values):
            shown = ", ".join(_code(v) for v in list(values)[:MAX_VALUES_SHOWN])
            bullets.append(f"{name}: {len(values)} distinct values, some differing only in case or spacing "
                           f"({shown}…)")
        numeric = c.get("numeric") or {}
        if numeric.get("negatives"):
            bullets.append(f"{name}: {numeric['negatives']} negative values (min {numeric['min']:g})")
    if personal:
        bullets.append(", ".join(personal) + ": likely **personal data** (values not shown)")
    spellings = sorted({t for c in cols for t in (c.get("null_tokens") or {})})
    if spellings:
        bullets.append("null spellings in use: " + ", ".join("empty" if t == "<empty>" else _code(t) for t in spellings))
    return {"type": "step", "agent": "profile", "phase": "profile", "route": route("profile"),
            "text": f"Scanned **{profile['table']}**. What stood out:", "bullets": bullets}


def spec_summary(spec: dict) -> List[str]:
    part = spec.get("partition")
    lines = [f"Key: {', '.join(_code(k) for k in spec['primary_key'])} — one row per key",
             f"Output: {len(spec['columns'])} typed columns" + (f" + partition {_code(part['name'])}" if part else "")]
    pii = [f"{_code(p['column'])} → {p['treatment']}" for p in spec.get("pii", [])]
    if pii:
        lines.append("Personal data: " + "; ".join(pii))
    rules = [r.get("id", r.get("name", "rule")) for r in spec.get("rules", [])]
    lines.append(f"Rules: schema, no rows lost, unique key" + (", " + ", ".join(rules) if rules else ""))
    info = [i.get("id", i.get("name", "")) for i in spec.get("info", [])]
    if info:
        lines.append("Reported, not failures: " + ", ".join(i for i in info if i))
    return lines


def spec_reused_step(dataset: str, spec_id: str, spec: dict, conventions: List[str]) -> dict:
    return {"type": "step", "agent": "spec", "phase": "spec", "route": route("spec"),
            "text": f"{_code(dataset)} has an approved contract (spec {_code(spec_id)}): "
                    f"{len(spec['columns'])} columns, key {', '.join(_code(k) for k in spec['primary_key'])}. "
                    "**Drafting the ETL against it.**",
            "bullets": [f"From memory: *{c}*" for c in conventions]}


def spec_gate(spec_id: str, spec: dict, needs_confirmation: List[str], changed: List[str] = ()) -> dict:
    return {"type": "gate", "kind": "spec", "id": spec_id, "dataset": spec["dataset"],
            "title": "Approve the contract first", "summary": spec_summary(spec), "changed": list(changed),
            "needs_confirmation": needs_confirmation,
            "guardrails": ["nothing runs yet", "the ETL script is drafted against this contract"],
            "code": json.dumps(spec, indent=2)}


def section(reply: str, heading: str) -> List[str]:
    """The '- ' bullets under a heading of an agent's reply (e.g. 'What the script does:')."""
    out, inside = [], False
    for line in reply.splitlines():
        stripped = line.strip()
        if stripped.lstrip("#*_ ").lower().startswith(heading.lower()):
            inside = True
            continue
        if inside:
            if stripped.startswith("**") or stripped.startswith("#"):
                break  # the next heading (often bold: **Assumptions:**), not a bullet
            if stripped.startswith(("-", "*", "•")):
                text = stripped.lstrip("-*• ").strip()
                if text and "NEEDS CONFIRMATION" not in text.upper() and text.lower() not in ("none", "nothing"):
                    out.append(text)
            elif stripped:
                break
    return out


def draft_step_and_gate(dataset: str, draft_id: str, script: str, reply: str,
                        needs_confirmation: List[str], where: dict, key: List[str]) -> List[dict]:
    plan = section(reply, "What the script does") or ["(the Script Writer gave no summary - read the script)"]
    changed = section(reply, "Changed since")
    # The plan appears once, on the card; the step only says a draft exists.
    step = {"type": "step", "agent": "script", "phase": "draft", "route": route("script"),
            "text": (f"Revised the draft for {_code(dataset)}" if changed else f"Drafted the ETL for {_code(dataset)}")
                    + f" — `{draft_id}`, {len(plan)} steps. Review it below."}
    gate = {"type": "gate", "kind": "draft", "id": draft_id, "dataset": dataset,
            "title": "Ready for your approval", "summary": plan, "changed": changed,
            "needs_confirmation": needs_confirmation,
            "guardrails": [f"writes only to `{where['staging_uri']}`", "`landing/` untouched",
                           f"one row per {', '.join(_code(k) for k in key)}"],
            "code": script}
    return [step, gate]


def run_step(run: dict, draft_id: str) -> dict:
    state, secs = run["JobRunState"], run.get("ExecutionTime", 0)
    text = f"Glue job `etl-copilot-job` **{state}** in {secs} s (run `{run['Id'][:16]}…`, draft `{draft_id}`)."
    if run.get("ErrorMessage"):
        text += f"\n\nError: `{run['ErrorMessage'][:300]}`"
    return {"type": "step", "agent": "execution", "phase": "run", "state": state, "route": route("execution"),
            "text": text}


def validation_step(validation: dict, attempt: Optional[int] = None) -> dict:
    rules = validation["rules"]
    passed = sum(r["passed"] for r in rules.values())
    table = ["| Rule | Result | Detail |", "|---|---|---|"]
    table += [f"| {name} | {'✅ passed' if r['passed'] else '❌ FAILED'} | {str(r['detail']).replace('|', '/')} |"
              for name, r in rules.items()]
    text = (f"Validated the output against the contract — **{passed}/{len(rules)} passed**"
            + (f" (attempt {attempt})" if attempt else "") + ".\n\n" + "\n".join(table))
    info = validation.get("info") or {}
    if info:
        text += "\n\nReported: " + " · ".join(f"{k} {v}" for k, v in info.items())
    return {"type": "step", "agent": "execution", "phase": "validate", "route": route("execution"), "text": text,
            "passed": validation["passed"], "dataset": validation.get("dataset"), "rules_passed": passed,
            "rules_total": len(rules), "attempt": attempt}


def docs_step(result: dict) -> dict:
    return {"type": "step", "agent": "documenter", "phase": "docs", "route": route("documenter"),
            "text": "Wrote the docs (every figure computed from the table; personal-data check passed):",
            "bullets": [f"Data dictionary → `{result['data_dictionary']}`",
                        f"Quality report → `{result['quality_report']}`"]
                       + ([f"Clean data as CSV → `{result['clean_csv']}`"] if result.get("clean_csv") else []),
            "files": {k: result[k] for k in ("data_dictionary", "quality_report", "clean_csv") if result.get(k)}}


SPEC_ID_RE = re.compile(r"SPEC_ID:\W*([0-9a-f]{12})")
DRAFT_ID_RE = re.compile(r"DRAFT_ID:\W*([0-9a-f]{12})")
