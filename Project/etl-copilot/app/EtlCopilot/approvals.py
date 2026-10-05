"""Human approval gate: the only way a draft becomes runnable.

An approval can only come from the engineer's own message, read by the entrypoint
(main.py) BEFORE any model sees it. Nothing a model writes (a reply, a tool argument,
a tool result) passes through here, so no prompt can talk an agent into approving.

Each approval names one draft id (a hash of the exact script the engineer reviewed)
and is used up by one run, within APPROVAL_TTL. Approvals are S3 objects under
approvals/ (step 4.4.1), so a runtime restart or a new microVM does not lose them. They
are written and removed with the caller's own identity (your login locally, the runtime
role when deployed) - never EtlCopilotExecutionRole, which the models' tools run as and
which has no access to approvals/.

"APPROVE SPEC <spec_id>" approves a proposed dataset spec (a hash of the exact contract
the engineer reviewed): it is installed as the dataset's contract right here, so a model
can never make a proposal binding.
"""
import json
import re
import threading
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

import memory
from config import APPROVALS_PREFIX, BUCKET, REGION
from tools.drafts import dataset_of_script, read_confirmations, read_draft
from tools.specs import promote_spec, read_spec_draft

REMEMBER_RE = re.compile(r"^\s*REMEMBER\s+([a-z][a-z0-9_]{1,39})\s*:\s*(\S.*)$", re.IGNORECASE | re.DOTALL)
FORGET_RE = re.compile(r"^\s*FORGET\s+([a-z][a-z0-9_]{1,39})\s+(\d{1,3})\s*$", re.IGNORECASE)
APPROVE_SPEC_RE = re.compile(r"^\s*APPROVE\s+SPEC\s+([0-9a-f]{12})\s*$", re.IGNORECASE)
REJECT_SPEC_RE = re.compile(r"^\s*REJECT\s+SPEC\s+([0-9a-f]{12})\b\s*(.*)$", re.IGNORECASE | re.DOTALL)
APPROVE_RE = re.compile(r"^\s*APPROVE\s+([0-9a-f]{12})\s*$", re.IGNORECASE)
REJECT_RE = re.compile(r"^\s*REJECT\s+([0-9a-f]{12})\b\s*(.*)$", re.IGNORECASE | re.DOTALL)

DRAFT_ID_RE = re.compile(r"^[0-9a-f]{12}$")
APPROVAL_TTL = timedelta(hours=24)  # an approval left unused for a day must be given again

_lock = threading.Lock()  # one consume at a time in this process; S3 IfMatch covers other processes
_client = None


def _s3():
    global _client
    if _client is None:  # the caller's own identity, deliberately not execution_session()
        _client = boto3.client("s3", region_name=REGION)
    return _client


def _key(draft_id: str) -> str:
    if not DRAFT_ID_RE.match(draft_id):
        raise ValueError(f"{draft_id!r} is not a draft id")
    return f"{APPROVALS_PREFIX}{draft_id}.json"


def approve(draft_id: str) -> None:
    body = {"draft_id": draft_id, "approved_at": datetime.now(timezone.utc).isoformat()}
    _s3().put_object(Bucket=BUCKET, Key=_key(draft_id), Body=json.dumps(body).encode("utf-8"),
                     ContentType="application/json")


def consume(draft_id: str) -> bool:
    """True if draft_id has a live approval; the approval is removed (one approval = one run)."""
    with _lock:
        try:
            obj = _s3().get_object(Bucket=BUCKET, Key=_key(draft_id))
        except (ValueError, ClientError):
            return False
        approved_at = datetime.fromisoformat(json.loads(obj["Body"].read())["approved_at"])
        try:  # delete only the object we just read, so two callers cannot both use it
            _s3().delete_object(Bucket=BUCKET, Key=_key(draft_id), IfMatch=obj["ETag"])
        except ClientError:
            return False
        return datetime.now(timezone.utc) - approved_at <= APPROVAL_TTL


def pending() -> list:
    """Draft ids with an approval waiting (expired ones included until consumed)."""
    pages = _s3().get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=APPROVALS_PREFIX)
    names = [o["Key"][len(APPROVALS_PREFIX):] for p in pages for o in p.get("Contents", [])]
    return sorted(n[:-5] for n in names if n.endswith(".json"))


def handle_human_message(text: str) -> str:
    """Turn the engineer's APPROVE / REJECT (SPEC) command into an instruction for the Supervisor.

    Only exact commands count ("APPROVE <id>" alone on the message), so a sentence that
    merely contains the word approve, or a quoted command, approves nothing.
    Any other message is returned unchanged.
    """
    match = APPROVE_SPEC_RE.match(text)
    if match:
        spec_id = match.group(1).lower()
        try:
            dataset = promote_spec(spec_id)
        except Exception as e:  # unknown id, changed content, invalid: nothing is installed
            return (f"The engineer sent APPROVE SPEC {spec_id}, but it could not be installed: {e}. "
                    "Tell the engineer exactly this; nothing was changed.")
        return (f"The engineer APPROVED spec {spec_id}: it is now the contract for dataset "
                f"{dataset}. Next, have the script writer draft the ETL for {dataset}, passing "
                "every convention the engineer stated, then present the draft for approval.")
    match = REJECT_SPEC_RE.match(text)
    if match:
        spec_id, reason = match.group(1).lower(), match.group(2).strip()
        _keep_request("spec", spec_id, reason)
        return (f"The engineer REJECTED spec {spec_id}. Reason: {reason or 'none given'}. "
                "Nothing was installed. If a reason was given, have the spec writer revise the "
                "proposal accordingly (its task receives the engineer's exact words automatically), "
                "then present the new spec for approval.")
    match = APPROVE_RE.match(text)
    if match:
        draft_id = match.group(1).lower()
        try:
            approve(draft_id)
        except Exception as e:  # nothing recorded, so nothing may run
            return (f"The engineer sent APPROVE {draft_id}, but the approval could not be recorded: {e}. "
                    "Tell the engineer exactly this and run nothing; they can send APPROVE again.")
        return (f"The engineer APPROVED draft {draft_id}. Have the execution agent run it, "
                "wait for it to finish, and validate the output." + _confirm_draft(draft_id))
    match = REMEMBER_RE.match(text)
    if match:
        dataset, convention = match.group(1).lower(), match.group(2).strip()
        if not memory.enabled():
            return "The engineer sent REMEMBER, but memory is not deployed yet: tell them nothing was saved."
        try:
            added = memory.remember(dataset, convention, source="REMEMBER")
        except ValueError as e:
            return f"The engineer sent REMEMBER, but it failed: {e}. Tell them nothing was saved."
        return (f"The engineer asked to remember for {dataset}: {convention!r}. "
                + ("It is saved and will be applied to every future spec and draft for that dataset."
                   if added else "It was already remembered.") + " Confirm this briefly.")
    match = FORGET_RE.match(text)
    if match:
        dataset, number = match.group(1).lower(), int(match.group(2))
        try:
            removed = memory.forget(dataset, number, source="FORGET") if memory.enabled() else ""
        except ValueError:  # not a dataset name
            removed = ""
        return (f"The engineer asked to forget convention {number} for {dataset}. "
                + (f"Removed: {removed!r}." if removed else
                   "There is no such convention (or memory is not deployed); nothing was removed.")
                + " Confirm this briefly and show the remaining list with show_conventions.")
    match = REJECT_RE.match(text)
    if match:
        draft_id, reason = match.group(1).lower(), match.group(2).strip()
        _keep_request("draft", draft_id, reason)
        return (f"The engineer REJECTED draft {draft_id}. Reason: {reason or 'none given'}. "
                "Do not run anything. If a reason was given, have the script writer revise "
                "the draft accordingly (its task receives the engineer's exact words automatically), "
                "then present the new draft for approval.")
    return text


# ---- the engineer's exact change requests -------------------------------------------------
# The Supervisor is a model and may shorten "change A, B and C" to "change A" when it briefs a
# writer. So code keeps the engineer's own words and hands them, verbatim, to the next writer
# call for that dataset (see take_request), whatever the Supervisor's task says.
_requests: dict = {}  # (kind, dataset) -> {"id": ..., "reason": ...}


def _keep_request(kind: str, item_id: str, reason: str) -> None:
    if not reason:
        return
    try:
        dataset = read_spec_draft(item_id)["dataset"] if kind == "spec" else dataset_of_script(read_draft(item_id))
    except Exception:
        return  # unknown id: there is nothing to revise
    if dataset:
        with _lock:
            _requests[(kind, dataset)] = {"id": item_id, "reason": reason}


def take_request(kind: str, dataset: str) -> str:
    """The engineer's pending change request for this writer and dataset, as a task block
    (used once), or '' if there is none."""
    with _lock:
        request = _requests.pop((kind, dataset), None)
    if not request:
        return ""
    return (f"\n\nThe engineer's exact change request for {kind} {request['id']}, word for word - "
            "apply EVERY part of it (it may list several changes separated by commas), and list "
            f"each one as its own bullet under 'Changed since {request['id']}:':\n\"{request['reason']}\"")


def _confirm_draft(draft_id: str) -> str:
    """APPROVE also confirms the draft's NEEDS CONFIRMATION points: remember them (code, not a model)."""
    try:
        recorded = read_confirmations(draft_id)
        points = recorded.get("needs_confirmation", [])
        if not points or not memory.enabled():
            return ""
        saved = [p for p in points if memory.remember(recorded["dataset"], p, source=f"APPROVE {draft_id}")]
    except Exception as e:  # memory trouble must never block an approval
        return f" (Memory could not record this draft's confirmed points: {e}.)"
    return (f" By approving, the engineer also confirmed {len(points)} point(s) for {recorded['dataset']}; "
            f"{len(saved)} new one(s) are now remembered for future drafts.") if saved else ""
