"""Engineer-confirmed conventions per dataset, kept in AgentCore Memory (step 4.1).

What is remembered: only what the engineer confirmed -
  - the NEEDS CONFIRMATION lines of a draft the engineer APPROVEd (they read them first),
  - anything sent with "REMEMBER <dataset>: <text>"; "FORGET <dataset> <n>" removes one.
Nothing a model decides on its own is ever written: writes happen only in approvals.py,
on the engineer's own message, before any model sees it.

Storage: one event per change (remember / forget), in actor "dataset-<name>", session
"conventions". The current list is the replay of those events in order, so the history
doubles as an audit trail. Calls use the caller's own AWS identity (your login locally,
the runtime role when deployed) - never EtlCopilotExecutionRole, which the models' tools
run as and which has no Memory permissions.

Without MEMORY_ETLCOPILOTMEMORY_ID (set by AgentCore once the memory is deployed) memory
is simply off: nothing is remembered and every list is empty.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import List

import boto3

from config import REGION
from tools.pii import find_personal_data
from tools.specs import paths

MEMORY_ENV = "MEMORY_ETLCOPILOTMEMORY_ID"  # AgentCore's name: MEMORY_<NAME>_ID
SESSION_ID = "conventions"
MAX_TEXT = 500

progress = logging.getLogger("etl_copilot")
_client = None


def memory_id() -> str:
    return os.getenv(MEMORY_ENV, "")


def enabled() -> bool:
    return bool(memory_id())


def _data_plane():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agentcore", region_name=REGION)
    return _client


def _actor(dataset: str) -> str:
    paths(dataset)  # validates the dataset name (letters, digits, _): safe as an actor id
    return f"dataset-{dataset}"


def _write(dataset: str, op: str, text: str, source: str) -> None:
    body = {"op": op, "text": text[:MAX_TEXT], "source": source,
            "at": datetime.now(timezone.utc).isoformat()}
    _data_plane().create_event(
        memoryId=memory_id(), actorId=_actor(dataset), sessionId=SESSION_ID,
        eventTimestamp=datetime.now(timezone.utc),
        payload=[{"conversational": {"content": {"text": json.dumps(body)}, "role": "OTHER"}}])
    progress.info("memory: %s %s: %s", op, dataset, text[:80])


def history(dataset: str) -> List[dict]:
    """Every remember/forget event for the dataset, oldest first."""
    if not enabled():
        return []
    events, token = [], None
    while True:
        kwargs = {"memoryId": memory_id(), "actorId": _actor(dataset), "sessionId": SESSION_ID,
                  "includePayloads": True, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = _data_plane().list_events(**kwargs)
        events += page.get("events", [])
        token = page.get("nextToken")
        if not token:
            break
    out = []
    for e in sorted(events, key=lambda e: e["eventTimestamp"]):
        for item in e.get("payload", []):
            try:
                out.append(json.loads(item["conversational"]["content"]["text"]))
            except (KeyError, ValueError):
                continue  # not one of ours
    return out


def conventions(dataset: str) -> List[str]:
    """The dataset's current conventions: the replay of its remember/forget history."""
    current: List[str] = []
    for event in history(dataset):
        if event.get("op") == "remember" and event["text"] not in current:
            current.append(event["text"])
        elif event.get("op") == "forget" and event["text"] in current:
            current.remove(event["text"])
    return current


def remember(dataset: str, text: str, source: str) -> bool:
    """Add a convention (no-op if already remembered). Returns True if it was added."""
    text = " ".join(text.split())
    if not enabled() or not text or text in conventions(dataset):
        return False
    if find_personal_data(text):  # memory is durable: never store a raw email or phone
        progress.info("memory: refused to remember a %s convention that contains personal data", dataset)
        return False
    _write(dataset, "remember", text, source)
    return True


def forget(dataset: str, number: int, source: str) -> str:
    """Remove the n-th (1-based) current convention. Returns its text, or '' if none."""
    current = conventions(dataset)
    if not enabled() or not 1 <= number <= len(current):
        return ""
    _write(dataset, "forget", current[number - 1], source)
    return current[number - 1]


def clean_confirmation(line: str) -> str:
    """'- NEEDS CONFIRMATION: **order_date**: `1900-01-01` -> null' -> 'order_date: 1900-01-01 -> null'."""
    line = re.sub(r"^[\s\-*•]*(NEEDS CONFIRMATION:)?\s*", "", line, flags=re.IGNORECASE)
    return " ".join(line.replace("**", "").replace("`", "").split())


def as_prompt(dataset: str) -> str:
    """The block the Spec Writer and Script Writer receive, or '' if there is nothing."""
    items = conventions(dataset)
    if not items:
        return ""
    return (f"\n\nEngineer-confirmed conventions for {dataset} (from memory - apply them, they are "
            "settled; do not list them under NEEDS CONFIRMATION):\n" + "\n".join(f"- {c}" for c in items))
