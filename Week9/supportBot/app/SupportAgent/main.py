from typing import Any
from collections import OrderedDict
from strands import Agent, tool
import asyncio
from strands.agent.conversation_manager.null_conversation_manager import NullConversationManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model
from mcp_client.client import get_streamable_http_mcp_client
import boto3

REGION = "us-west-2"                 # match your project's region
KB_ID = "NUPVAWA8U9"     # the S3 Vectors KB ID from Step 2
app = BedrockAgentCoreApp()
log = app.logger

# Define a Streamable HTTP MCP Client
mcp_clients = [get_streamable_http_mcp_client()]

DEFAULT_SYSTEM_PROMPT = """
You are a customer support assistant for Acme.
Use get_order_status when a customer gives an order ID.
Use get_product_faq for questions about product features, warranty, or specs
(AcmePhone X, AcmeBook Pro, AcmeBuds).
If you don't know, say so — never invent order or product details.
"""

# Define a collection of tools used by the model
tools = []

_INLINE_FUNCTION_NAMES = set()

ORDERS = {
    "1001": "Order 1001: Shipped via FedEx (tracking FX123456789), ETA 2026-09-08. Items: AcmePhone X (1).",
    "1002": "Order 1002: Processing, ETA 2026-09-12. Items: AcmeBook Pro (1), AcmeBuds (1). No tracking yet.",
    "1003": "Order 1003: Delivered 2026-09-01 via UPS (tracking 1Z999AA10123456784). Items: AcmeBuds (2).",
}

@tool
def get_order_status(order_id: str) -> str:
    """Look up the status of a customer's order by its order ID.

    Args:
        order_id: The order number the customer provides, e.g. '1001'.
    """
    return ORDERS.get(order_id.strip(), f"No order found with ID '{order_id}'.")
tools.append(get_order_status)

@tool
def get_product_faq(product_name: str) -> str:
    """Answer questions about Acme products (features, warranty, specs) by
    retrieving relevant passages from the product FAQ knowledge base.

    Args:
        product_name: The product or topic to look up, e.g. 'AcmePhone X battery'.
    """
    client = boto3.client("bedrock-agent-runtime", region_name=REGION)
    resp = client.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": product_name},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 3}},
    )
    chunks = [r["content"]["text"] for r in resp.get("retrievalResults", [])]
    return "\n\n".join(chunks) if chunks else "No FAQ entry found for that product."
tools.append(get_product_faq)



# Add MCP client to tools if available
for mcp_client in mcp_clients:
    if mcp_client:
        tools.append(mcp_client)


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
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            tools=tools,
            conversation_manager=_make_conversation_manager(),
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



@app.entrypoint
async def invoke(payload, context):
    log.info("Invoking Agent.....")


    session_id = getattr(context, 'session_id', 'default-session')
    agent = get_or_create_agent(session_id)

    prompt = _extract_prompt(payload)


    async for event in agent.stream_async(
        prompt,
    ):
        if not isinstance(event, dict) or "event" not in event:
            continue
        cbs = event["event"].get("contentBlockStart")
        if cbs is not None and not cbs.get("start"):
            continue
        yield event


if __name__ == "__main__":
    app.run()
