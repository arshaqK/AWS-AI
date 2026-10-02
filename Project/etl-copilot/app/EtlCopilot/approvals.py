"""Human approval gate: the only way a draft becomes runnable.

An approval can only come from the engineer's own message, read by the entrypoint
(main.py) BEFORE any model sees it. Nothing a model writes (a reply, a tool argument,
a tool result) passes through here, so no prompt can talk an agent into approving.

Each approval names one draft id (a hash of the exact script the engineer reviewed)
and is used up by one run. Approvals live in memory: restarting the server clears them.

"APPROVE SPEC <spec_id>" approves a proposed dataset spec (a hash of the exact contract
the engineer reviewed): it is installed as the dataset's contract right here, so a model
can never make a proposal binding.
"""
import re
import threading

from tools.specs import promote_spec

APPROVE_SPEC_RE = re.compile(r"^\s*APPROVE\s+SPEC\s+([0-9a-f]{12})\s*$", re.IGNORECASE)
REJECT_SPEC_RE = re.compile(r"^\s*REJECT\s+SPEC\s+([0-9a-f]{12})\b\s*(.*)$", re.IGNORECASE | re.DOTALL)
APPROVE_RE = re.compile(r"^\s*APPROVE\s+([0-9a-f]{12})\s*$", re.IGNORECASE)
REJECT_RE = re.compile(r"^\s*REJECT\s+([0-9a-f]{12})\b\s*(.*)$", re.IGNORECASE | re.DOTALL)

_lock = threading.Lock()
_approved: set = set()


def approve(draft_id: str) -> None:
    with _lock:
        _approved.add(draft_id)


def consume(draft_id: str) -> bool:
    """True if draft_id was approved; the approval is removed (one approval = one run)."""
    with _lock:
        if draft_id in _approved:
            _approved.remove(draft_id)
            return True
        return False


def pending() -> list:
    with _lock:
        return sorted(_approved)


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
        return (f"The engineer REJECTED spec {spec_id}. Reason: {reason or 'none given'}. "
                "Nothing was installed. If a reason was given, have the spec writer revise the "
                "proposal accordingly, then present the new spec for approval.")
    match = APPROVE_RE.match(text)
    if match:
        draft_id = match.group(1).lower()
        approve(draft_id)
        return (f"The engineer APPROVED draft {draft_id}. Have the execution agent run it, "
                "wait for it to finish, and validate the output.")
    match = REJECT_RE.match(text)
    if match:
        draft_id, reason = match.group(1).lower(), match.group(2).strip()
        return (f"The engineer REJECTED draft {draft_id}. Reason: {reason or 'none given'}. "
                "Do not run anything. If a reason was given, have the script writer revise "
                "the draft accordingly, then present the new draft for approval.")
    return text
