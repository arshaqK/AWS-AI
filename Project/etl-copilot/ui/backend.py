"""Talking to ETL Copilot from the console: one turn at a time, streamed in a background thread.

Two backends, same events (see app/EtlCopilot/narration.py):
  LocalBackend    - `agentcore dev` on localhost:8081 (the demo default)
  DeployedBackend - the AgentCore Runtime, signed with your own AWS login

A turn runs in a worker thread that only appends to its Turn object; the Streamlit script
reads the Turn on each rerun, so a 4-minute Glue run never freezes the page.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Iterator, List, Optional

ROOT = Path(__file__).resolve().parents[1]
REGION = "us-west-2"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:481719141347:runtime/EtlCopilot_EtlCopilot-agiQ51AyLD"
LOCAL_URL = "http://localhost:8081"
TIMEOUT_S = 900


def load_local_env() -> None:
    """The deployed memory's id (agentcore/.env.local), so the console reads the same memory."""
    env = ROOT / "agentcore" / ".env.local"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def new_session_id() -> str:
    return f"etl-copilot-console-{uuid.uuid4().hex}"  # 52 characters; AgentCore needs 33+


def parse_sse(lines: Iterator[str]) -> Iterator[dict]:
    """Narration and heartbeat events from a server-sent-events stream; token text is skipped
    (the final event carries the whole reply)."""
    for line in lines:
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            continue
        if isinstance(event, dict) and "narration" in event:
            yield event["narration"]
        elif isinstance(event, dict) and "status" in event:
            yield {"type": "heartbeat", "elapsed_s": event.get("elapsed_s", 0)}


class LocalBackend:
    name = "Local (agentcore dev)"

    def __init__(self, url: str = LOCAL_URL):
        self.url = url

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/ping", timeout=1) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def stream(self, prompt: str, session_id: str) -> Iterator[dict]:
        req = urllib.request.Request(
            f"{self.url}/invocations", data=json.dumps({"prompt": prompt}).encode(),
            headers={"Content-Type": "application/json",
                     "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            yield from parse_sse(raw.decode("utf-8", "replace").rstrip("\n") for raw in resp)


class DeployedBackend:
    name = "Deployed (AgentCore Runtime)"

    def __init__(self, arn: str = RUNTIME_ARN):
        self.arn = arn

    def healthy(self) -> bool:
        try:
            import boto3
            boto3.client("sts", region_name=REGION).get_caller_identity()
            return True
        except Exception:
            return False

    def stream(self, prompt: str, session_id: str) -> Iterator[dict]:
        import boto3
        from botocore.config import Config
        client = boto3.client("bedrock-agentcore", region_name=REGION,
                              config=Config(read_timeout=TIMEOUT_S, retries={"max_attempts": 0}))
        resp = client.invoke_agent_runtime(agentRuntimeArn=self.arn, runtimeSessionId=session_id,
                                           payload=json.dumps({"prompt": prompt}).encode())
        yield from parse_sse(raw.decode("utf-8", "replace") for raw in resp["response"].iter_lines())


class Turn:
    """One message to the agent and everything it streamed back. Written by the worker thread only."""

    def __init__(self, prompt: str):
        self.prompt = prompt
        self.events: List[dict] = []
        self.done = False
        self.error: Optional[str] = None
        self.started = time.time()
        self.last_event_at = self.started
        self.activity = "Supervisor is reading your message"  # what is happening right now
        self.activity_agent = "supervisor"

    @property
    def elapsed(self) -> int:
        return int(time.time() - self.started)


def start_turn(backend, prompt: str, session_id: str) -> Turn:
    turn = Turn(prompt)

    def work():
        try:
            for event in backend.stream(prompt, session_id):
                turn.last_event_at = time.time()
                if event["type"] == "activity":  # the status line, not the feed
                    turn.activity = event.get("text") or turn.activity
                    turn.activity_agent = event.get("agent") or "supervisor"
                elif event["type"] != "heartbeat":
                    turn.events.append(event)  # list.append is atomic: safe to read while it grows
        except Exception as e:  # shown in the feed; the worker never raises into Streamlit
            turn.error = f"{type(e).__name__}: {e}"
        finally:
            turn.done = True

    threading.Thread(target=work, daemon=True, name="etl-copilot-turn").start()
    return turn
