import asyncio
import logging
import sys
import time
from typing import Any
from collections import OrderedDict
from strands import Agent
from strands.agent.conversation_manager.null_conversation_manager import NullConversationManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model
import json
import approvals
from strands import tool
from agents.execution_agent import execution_agent
from agents.script_writer import script_writer
from agents.spec_writer import spec_writer
from tools.profile import get_dataset_profile
from tools.specs import list_datasets as _list_datasets

app = BedrockAgentCoreApp()
log = app.logger

# Progress lines from the tools (job started, job state, draft saved...) go to the server
# terminal, so long silent steps are visible while you test.
_progress = logging.getLogger("etl_copilot")
if not _progress.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("[etl-copilot] %(message)s"))
    _progress.addHandler(_handler)
    _progress.setLevel(logging.INFO)
    _progress.propagate = False

SUPERVISOR_PROMPT = """You are ETL Copilot, an assistant for data engineers. You coordinate the
specialists that turn a raw vendor CSV in s3://etl-copilot-ak/landing/<dataset>/ into a clean,
validated table etl_copilot_clean.<dataset>. You never write specs, ETL code or run jobs yourself.

## Your tools
- list_datasets: every dataset (a folder under landing/), and whether it has a raw table and
  an approved spec. Use it whenever you are not sure which dataset is meant.
- spec_writer(dataset, task): proposes or revises a dataset's spec - its contract (columns,
  types, key, PII handling, cleaning and validation rules). Returns SPEC_ID and a summary.
- script_writer(dataset, task): drafts or revises the ETL script for a dataset with an approved
  spec (it profiles the data itself). Returns DRAFT_ID, what the script does, and assumptions.
- execution_agent(draft_id): runs an APPROVED draft on Glue, waits, and validates the output.
- get_dataset_profile(dataset): only to answer the engineer's questions about raw data.

## Rules that are never broken
- Only the engineer can approve, by sending exactly "APPROVE SPEC <spec_id>" for a spec or
  "APPROVE <draft_id>" for a script. You cannot approve anything, and you never say something
  is approved unless the message tells you so.
- Never call execution_agent in the same turn that a draft was created or revised, and never
  call script_writer in the same turn a spec was proposed: present it and end your turn so
  the engineer can review it.
- Copy spec and draft ids exactly from tool output. Never invent or shorten one.
- Always pass the dataset name exactly as list_datasets shows it.

## Flow
0. Work out which dataset the engineer means (list_datasets if unclear). If it has no raw
   table, tell the engineer to upload the CSV to landing/<dataset>/ and run the crawler.
1. The engineer asks to onboard / clean a dataset:
   - It has no approved spec: call spec_writer with their request, present the spec
     (format S) and stop.
   - It has an approved spec: call script_writer once with their request and every
     convention they stated, word for word. Present the draft (format A) and stop.
2. A message says the engineer APPROVED spec X for dataset D: call script_writer for D with
   every convention the engineer has stated so far. Present the draft (format A) and stop.
3. A message says the engineer REJECTED spec X with a reason: revise via spec_writer (pass
   the spec id and the reason), present the new spec (format S) and stop.
4. A message says the engineer APPROVED draft X: call execution_agent for draft X.
   - Validation PASSED: report the result (format B). Done.
   - The job failed or a rule failed: call script_writer to revise draft X, passing the
     execution report word for word. Present the new draft (format A, with what changed) and stop.
   - REFUSED or BUSY: report it as is. Do not retry.
5. A message says the engineer REJECTED draft X with a reason: revise via script_writer with that
   reason and present the new draft. Without a reason: acknowledge and wait for instructions.
6. Any other message that changes requirements: if it changes the contract (columns, types,
   key, PII handling, validation rules), revise the spec via spec_writer (format S); otherwise
   revise the current draft via script_writer (format A).
7. If three revisions in a row have failed, stop and ask the engineer how to proceed.

## Format S - a spec ready for review
**Spec `<id>` for `<dataset>` ready for review**
<the spec writer's summary: key, columns, partition, PII, rules - one line each>
Needs your confirmation:
- <every NEEDS CONFIRMATION line, word for word, or "nothing">
Reply `APPROVE SPEC <id>` to make it the contract, or `REJECT SPEC <id> <reason>`.

## Format A - a draft ready for review
**Draft `<id>` ready for review**
What it will do:
- <the script writer's bullets, shortened to one line each>
Needs your confirmation:
- <every NEEDS CONFIRMATION line, word for word, or "nothing">
(For a revision, add "Changed since `<old id>`:" with the changes.)
Reply `APPROVE <id>` to run it on Glue, or `REJECT <id> <reason>`.

## Format B - a finished run
**Draft `<id>` ran and passed validation** (dataset `<dataset>`)
Run: <run id> - <state>
Rules: one line per rule (passed / failed - detail)
Info: <the info counts>
Output: s3://etl-copilot-ak/staging/<dataset>/ (table etl_copilot_clean.<dataset>)

Be concise and technical.
"""


@tool
def list_datasets() -> str:
    """List every dataset: its landing folder, whether its raw table exists (crawler has run),
    and whether it has an approved spec. Returns JSON."""
    return json.dumps(_list_datasets())


# The Supervisor's specialists. Only execution_agent can start a Glue job, and only for a
# draft the engineer approved; only the engineer's APPROVE SPEC installs a spec (both
# enforced in code by approvals.py, not by this prompt).
tools = [list_datasets, spec_writer, script_writer, execution_agent, get_dataset_profile]

_INLINE_FUNCTION_NAMES = set()


def _make_conversation_manager():
    return NullConversationManager()

# Reuses one Agent per session_id so each session keeps its own in-process
# conversation history (best-effort; resets on cold start). The cache is bounded
# to 128 sessions with LRU eviction (least-recently-used is dropped and its
# history reset) so a single process serving many sessions cannot leak history
# between them or grow without limit. For durable history, attach a session manager.
def agent_factory():
    cache = OrderedDict()
    def get_or_create_agent(session_id):
        if session_id in cache:
            cache.move_to_end(session_id)
            return cache[session_id]
        if len(cache) >= 128:
            cache.popitem(last=False)
        cache[session_id] = Agent(
            model=load_model(),
            system_prompt=SUPERVISOR_PROMPT,
            tools=tools,
            conversation_manager=_make_conversation_manager(),
            # Events are streamed back via stream_async; the default handler would also print
            # every token to server stdout, which crashes on Windows (cp1252) for emoji/non-Latin text.
            callback_handler=None,
            hooks=[
            ],
        )
        return cache[session_id]
    return get_or_create_agent
get_or_create_agent = agent_factory()


def strip_trailing_tool_use(messages: Any) -> list[dict]:
    """Strip toolUse blocks from the tail until the last message has none."""
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")

    messages = list(messages)
    while messages:
        last = messages[-1]
        if not isinstance(last, dict):
            raise ValueError("each message must be an object")
        original_content = last.get("content", [])
        if not isinstance(original_content, list) or not all(isinstance(block, dict) for block in original_content):
            raise ValueError("each message content value must be a list of content blocks")

        content = [block for block in original_content if "toolUse" not in block]
        if len(content) == len(original_content):
            break
        if content:
            messages[-1] = {**last, "content": content}
            break
        messages.pop()

    return messages


def _extract_prompt(payload: dict):
    """Accept validated harness messages, tool results, or a plain prompt string."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if "messages" in payload:
        return strip_trailing_tool_use(payload["messages"])
    if "tool_results" in payload:
        tool_results = payload["tool_results"]
        if not isinstance(tool_results, list) or not all(
            isinstance(tool_result, dict) and isinstance(tool_result.get("toolUseId"), str)
            for tool_result in tool_results
        ):
            raise ValueError("tool_results must contain objects with a toolUseId string")
        return [{"role": "user", "content": [{"toolResult": {
            "toolUseId": tr["toolUseId"],
            "status": tr.get("status", "success"),
            "content": tr.get("content", []),
        }} for tr in tool_results]}]
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")
    return prompt


def _has_inline_function_call(messages) -> bool:
    """Return True if messages contains an assistant toolUse for an inline function tool."""
    if not _INLINE_FUNCTION_NAMES or not isinstance(messages, list):
        return False
    for msg in messages:
        if msg.get("role") == "assistant":
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("toolUse", {}).get("name") in _INLINE_FUNCTION_NAMES:
                    return True
    return False


def _is_inline_function_call(event: dict) -> bool:
    """Check if a contentBlockStart event is for an inline function tool."""
    if not _INLINE_FUNCTION_NAMES:
        return False
    cbs = event.get("contentBlockStart", {})
    start = cbs.get("start", {})
    tool_use = start.get("toolUse") if isinstance(start, dict) else None
    return tool_use is not None and tool_use.get("name") in _INLINE_FUNCTION_NAMES


HEARTBEAT_S = 20


async def _with_heartbeat(events):
    """Pass events through, adding {"status": "working"} whenever none arrived for HEARTBEAT_S.

    A Glue run plus validation is minutes of silence, and HTTP clients drop a response that
    sends nothing for long enough (Node's fetch, used by `agentcore dev`, gives up at 300s).
    Clients ignore events without text, so the heartbeat never shows up in the reply.
    """
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    async def pump():
        try:
            async for event in events:
                await queue.put(event)
        except Exception as e:  # handed to the consumer, which re-raises it
            await queue.put(e)
        finally:
            await queue.put(done)

    task = asyncio.create_task(pump())
    started = time.monotonic()
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), HEARTBEAT_S)
            except asyncio.TimeoutError:
                yield {"status": "working", "elapsed_s": int(time.monotonic() - started)}
                continue
            if item is done:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        task.cancel()


@app.entrypoint
async def invoke(payload, context):
    log.info("Invoking Agent.....")


    session_id = getattr(context, 'session_id', 'default-session')
    agent = get_or_create_agent(session_id)

    prompt = _extract_prompt(payload)
    # The approval gate: an engineer's "APPROVE <draft_id>" is recorded here, before any
    # model sees the message, so no model output can ever create an approval.
    if isinstance(prompt, str):
        prompt = approvals.handle_human_message(prompt)
        _progress.info("engineer: %s", prompt[:120])


    async for event in _with_heartbeat(agent.stream_async(prompt)):
        if not isinstance(event, dict):
            continue
        if "status" in event:  # heartbeat
            yield event
            continue
        if "event" not in event:
            continue
        cbs = event["event"].get("contentBlockStart")
        if cbs is not None and not cbs.get("start"):
            continue
        yield event


if __name__ == "__main__":
    app.run()
