#!/usr/bin/env python3
"""
RAG chatbot over an Amazon Bedrock Knowledge Base.

Uses the RetrieveAndGenerate API to answer questions grounded in your documents,
and can fall back to a plain (non-RAG) model call so you can compare the two
(deliverable b in the task).

Setup:
    pip install boto3
    aws configure            # or set AWS_PROFILE / env credentials
    export KB_ID=XXXXXXXXXX  # your Knowledge Base ID from the Bedrock console
    export AWS_REGION=us-east-1

Run:
    python rag_chatbot.py

In-chat commands:
    /model <key>   switch generation model (see MODELS below)
    /models        list available model keys
    /direct        ask the current model WITHOUT the knowledge base (for comparison)
    /compare       ask the same question both ways, side by side
    /reset         start a fresh conversation (clears session memory)
    /quit          exit
"""

import os
import sys
import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REGION = os.environ.get("AWS_REGION", "us-east-1")
KB_ID = os.environ.get("KB_ID", "HBDAW1STS4")
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "hierarchical_output.txt")  # transcript appended here

# Model keys -> Bedrock inference-profile IDs.
# NOTE: Claude Haiku 4.5 (and other current Claude models) are ONLY invokable
# via an inference profile, not the bare model ID. The "us." prefix is the US
# cross-region profile; swap to "eu.", "apac.", or "global." to match your region.
MODELS = {
    "haiku4.5":  "us.anthropic.claude-haiku-4-5-20251001-v1:0",   # default: fast + cheap
    "sonnet4.5": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",  # stronger reasoning
    "nova-pro":  "us.amazon.nova-pro-v1:0",                       # Amazon's own model
    "nova-lite": "us.amazon.nova-lite-v1:0",
    # Titan Text is a bare ID (no inference profile needed):
    "titan":     "amazon.titan-text-premier-v1:0",
}
current_model = "haiku4.5"

# How many chunks to pull per query. Match what you tested in the console.
NUM_RESULTS = 5

# This is the key to making the 5 "unknowable" questions behave. The template
# must contain the $search_results$ placeholder; the user's question is injected
# automatically by the RetrieveAndGenerate API.
PROMPT_TEMPLATE = """You are a precise assistant. Answer the user's question using ONLY the
search results below. Do not use any outside knowledge.

If the search results do not contain enough information to answer, reply exactly:
"I don't know based on the provided documents."

Do not guess, do not fill gaps with general knowledge, and do not apologize at length.

Search results:
$search_results$
"""

agent_rt = boto3.client("bedrock-agent-runtime", region_name=REGION)
runtime = boto3.client("bedrock-runtime", region_name=REGION)

# Session id lets RetrieveAndGenerate remember the conversation across turns.
session_id = None


class Tee:
    """Mirror everything written to stdout into a file too, so the whole session
    (questions, answers, retrieved chunks) is saved for your demo before you
    tear the Knowledge Base down."""

    def __init__(self, path):
        self.file = open(path, "a", encoding="utf-8")
        self.stdout = sys.__stdout__

    def write(self, data):
        self.stdout.write(data)
        self.file.write(data)

    def flush(self):
        self.stdout.flush()
        self.file.flush()


# ---------------------------------------------------------------------------
# Core calls
# ---------------------------------------------------------------------------
def retrieve_chunks(question: str) -> list:
    """Call the Retrieve API directly to get the TRUE ranked list of chunks the
    vector search returned. This is the correct signal for measuring retrieval
    accuracy (deliverable a) — unlike citations, it does not depend on whether
    the model chose to cite them."""
    resp = agent_rt.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": question},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": NUM_RESULTS}
        },
    )
    out = []
    for r in resp.get("retrievalResults", []):
        loc = r.get("location", {})
        uri = (
            loc.get("s3Location", {}).get("uri")
            or loc.get("webLocation", {}).get("url")
            or "unknown"
        )
        score = r.get("score")
        snippet = r.get("content", {}).get("text", "")[:160].replace("\n", " ")
        out.append((uri, score, snippet))
    return out


def rag_answer(question: str) -> dict:
    """Retrieve from the KB and generate a grounded answer."""
    global session_id

    request = {
        "input": {"text": question},
        "retrieveAndGenerateConfiguration": {
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": KB_ID,
                "modelArn": MODELS[current_model],
                "retrievalConfiguration": {
                    "vectorSearchConfiguration": {"numberOfResults": NUM_RESULTS}
                },
                "generationConfiguration": {
                    "promptTemplate": {"textPromptTemplate": PROMPT_TEMPLATE},
                    "inferenceConfig": {
                        "textInferenceConfig": {"temperature": 0.0, "maxTokens": 1024}
                    },
                },
            },
        },
    }
    # Continue the same session if we have one.
    if session_id:
        request["sessionId"] = session_id

    resp = agent_rt.retrieve_and_generate(**request)
    session_id = resp.get("sessionId", session_id)

    answer = resp["output"]["text"]

    # Which chunks the model actually CITED (a subset of what was retrieved,
    # and often empty when a custom prompt template is used).
    cited = []
    for citation in resp.get("citations", []):
        for ref in citation.get("retrievedReferences", []):
            loc = ref.get("location", {})
            uri = (
                loc.get("s3Location", {}).get("uri")
                or loc.get("webLocation", {}).get("url")
                or "unknown"
            )
            cited.append(uri)

    # The TRUE retrieved set, fetched separately via the Retrieve API.
    retrieved = retrieve_chunks(question)

    return {"answer": answer, "retrieved": retrieved, "cited": cited}


def direct_answer(question: str) -> str:
    """Ask the current model WITHOUT the knowledge base (no grounding)."""
    resp = runtime.converse(
        modelId=MODELS[current_model],
        messages=[{"role": "user", "content": [{"text": question}]}],
        inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
    )
    return resp["output"]["message"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------
def show_rag(result: dict):
    print(f"\n[RAG · {current_model}]\n{result['answer']}\n")
    retrieved = result["retrieved"]
    if retrieved:
        print("  Retrieved chunks (Retrieve API — ground truth for accuracy):")
        for i, (uri, score, snippet) in enumerate(retrieved, 1):
            score_str = f"  (score {score:.3f})" if isinstance(score, (int, float)) else ""
            print(f"    {i}. {uri}{score_str}")
            if snippet:
                print(f"       \"{snippet}...\"")
       
    else:
        # Now this only fires when the vector search genuinely returned nothing.
        print("  (Retrieve API returned no chunks — nothing in the KB matched)")
    print()
    


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    global current_model, session_id

    if KB_ID.startswith("REPLACE"):
        print("Set your KB_ID first:  export KB_ID=xxxxxxxxxx")
        sys.exit(1)

    import datetime
    sys.stdout = Tee(OUTPUT_FILE)
    sys.stdout.file.write(
        f"\n{'=' * 70}\nSession {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        f"  (model={current_model})\n{'=' * 70}\n"
    )

    print(f"RAG chatbot ready. Region={REGION}  KB={KB_ID}  Model={current_model}")
    print("Type a question, or /models, /model <key>, /direct, /compare, /reset, /quit.\n")

    while True:
        print("=" * 70)
        print()
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        # The terminal echoes the typed line, but stdout doesn't see it — so
        # write it into the transcript ourselves to keep the log complete.
        if isinstance(sys.stdout, Tee):
            sys.stdout.file.write(f"you> {line}\n")

        # ---- commands ----
        if line in ("/quit", "/exit"):
            break
        if line == "/models":
            print("  " + "  ".join(MODELS.keys()) + "\n")
            continue
        if line.startswith("/model "):
            key = line.split(maxsplit=1)[1].strip()
            if key in MODELS:
                current_model = key
                print(f"  switched to {key}\n")
            else:
                print(f"  unknown model. options: {', '.join(MODELS)}\n")
            continue
        if line == "/reset":
            session_id = None
            print("  conversation reset\n")
            continue

        try:
            if line.startswith("/direct "):
                q = line.split(maxsplit=1)[1]
                print(f"\n[DIRECT · {current_model} · no KB]\n{direct_answer(q)}\n")
                continue
            if line.startswith("/compare "):
                q = line.split(maxsplit=1)[1]
                show_rag(rag_answer(q))
                print(f"[DIRECT · {current_model} · no KB]\n{direct_answer(q)}\n")
                continue

            # ---- normal question -> RAG ----
            show_rag(rag_answer(line))

        except ClientError as e:
            code = e.response["Error"]["Code"]
            msg = e.response["Error"]["Message"]
            print(f"  AWS error [{code}]: {msg}")
            if "on-demand throughput" in msg or "inference profile" in msg.lower():
                print("  -> This model needs an inference-profile ID (e.g. us.anthropic...),")
                print("     or try passing the full inference-profile ARN as modelArn.\n")
            else:
                print()

    print("bye")
    if isinstance(sys.stdout, Tee):
        sys.stdout.flush()
        sys.stdout.file.close()


if __name__ == "__main__":
    main()
