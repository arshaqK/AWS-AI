"""Watch one turn's stream event by event: narration steps, gates, heartbeats, final reply.

Local (start `agentcore dev --port 8081 --logs --skip-deploy` first):
    uv run python ../../tests/stream_events.py --session <id> "Onboard support_tickets"
Deployed:
    uv run python ../../tests/stream_events.py --deployed --session <id> "APPROVE <draft_id>"

Session ids must be at least 33 characters; reuse the same one for the APPROVE turn.
Exits 1 if a gate names a draft or spec that does not exist in S3.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(os.getenv("ETL_COPILOT_APP", ROOT / "app" / "EtlCopilot"))))

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:481719141347:runtime/EtlCopilot_EtlCopilot-agiQ51AyLD"


def local_lines(prompt: str, session: str, port: int):
    req = urllib.request.Request(
        f"http://localhost:{port}/invocations", data=json.dumps({"prompt": prompt}).encode(),
        headers={"Content-Type": "application/json",
                 "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session})
    with urllib.request.urlopen(req, timeout=900) as resp:
        for raw in resp:
            yield raw.decode("utf-8", "replace").rstrip("\n")


def deployed_lines(prompt: str, session: str):
    import boto3
    from botocore.config import Config
    client = boto3.client("bedrock-agentcore", region_name="us-west-2",
                          config=Config(read_timeout=900, retries={"max_attempts": 0}))
    resp = client.invoke_agent_runtime(agentRuntimeArn=RUNTIME_ARN, runtimeSessionId=session,
                                       payload=json.dumps({"prompt": prompt}).encode())
    for raw in resp["response"].iter_lines():
        yield raw.decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--session", required=True)
    ap.add_argument("--deployed", action="store_true")
    ap.add_argument("--port", type=int, default=8081)
    args = ap.parse_args()
    if len(args.session) < 33:
        ap.error("--session must be at least 33 characters")

    lines = deployed_lines(args.prompt, args.session) if args.deployed else local_lines(args.prompt, args.session, args.port)
    started, order, gates = time.time(), [], []
    for line in lines:
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            continue
        t = f"{time.time() - started:6.0f}s"
        if not isinstance(event, dict):
            continue
        if "status" in event:
            print(f"{t}  · heartbeat ({event.get('elapsed_s')}s)")
        elif "narration" in event:
            n = event["narration"]
            order.append(n["type"] if n["type"] != "step" else f"step:{n['agent']}")
            if n["type"] == "step":
                print(f"{t}  STEP  {n.get('route', '')}  {n.get('text', '')[:110]}")
                for b in n.get("bullets", [])[:8]:
                    print(f"            - {b[:120]}")
            elif n["type"] == "gate":
                gates.append(n)
                print(f"{t}  GATE  {n['kind']} {n['id']} ({n['dataset']}) - {len(n['summary'])} summary lines, "
                      f"{len(n.get('needs_confirmation', []))} to confirm, code {len(n.get('code', ''))} chars")
            elif n["type"] == "final":
                print(f"{t}  FINAL\n{n['text']}")
    print("\norder:", " → ".join(order))

    ok = True
    for g in gates:  # every gate must name something that really exists
        from tools.drafts import draft_id_for, read_draft
        from tools.specs import read_spec_draft
        try:
            exists = (draft_id_for(read_draft(g["id"])) == g["id"]) if g["kind"] == "draft" else bool(read_spec_draft(g["id"]))
        except Exception:
            exists = False
        print(f"{'PASS' if exists else 'FAIL'}  gate {g['kind']} {g['id']} exists in S3")
        ok &= exists
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
