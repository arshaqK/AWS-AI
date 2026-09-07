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

# Mock return policy (the "new Lambda" for the Returns sub-agent).
RETURN_POLICY = {
    "acmephone x": "AcmePhone X: returnable within 14 days of delivery if undamaged. "
                   "10% restocking fee if opened. Refund to original payment in 5-7 business days.",
    "acmebook pro": "AcmeBook Pro: 30-day return window for a full refund in original condition "
                    "with all accessories. No restocking fee. Refund in 5-7 business days.",
    "acmebuds": "AcmeBuds: returnable within 30 days ONLY if unopened/sealed (hygiene item). "
                "Opened earbuds are not returnable except for a warranty defect.",
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

@tool
def get_return_policy(product_name: str) -> str:
    """Return the return-eligibility and refund policy for an Acme product.

    Args:
        product_name: e.g. 'AcmePhone X', 'AcmeBook Pro', 'AcmeBuds'.
    """
    key = product_name.strip().lower()
    for name, policy in RETURN_POLICY.items():
        if name in key or key in name:
            return policy
    return ("No specific return policy found for that product. General policy: "
            "most items are returnable within 30 days in original condition.")


# ---------------------------------------------------------------------------
# Sub-agents, exposed to the supervisor AS TOOLS.
# The docstrings below are what the supervisor reads to decide routing, so they
# are written to describe each sub-agent's domain crisply.
# ---------------------------------------------------------------------------

ORDER_PROMPT = (
    "You are the Order sub-agent for Acme. You handle order status and shipping "
    "questions only. Use get_order_status when an order ID is provided. "
    "Do not answer returns/refund questions."
)

RETURNS_PROMPT = (
    "You are the Returns sub-agent for Acme. You handle return eligibility and "
    "refund questions only. "
    "If the customer names a product, call get_return_policy with that product "
    "name directly — do NOT ask for an order number, you do not need one. "
    "Only if the customer gives an order ID but NOT a product should you call "
    "get_order_status first to find the product, then get_return_policy. "
    "Do not answer order-tracking questions."
)

@tool
def order_agent(query: str) -> str:
    """Handle ORDER STATUS and SHIPPING questions: where an order is, whether it
    has shipped, tracking numbers, estimated delivery. Use for anything about the
    progress or location of a purchase the customer has already placed.

    Args:
        query: the customer's order/shipping question, verbatim.
    """
    agent = Agent(model=load_model(), system_prompt=ORDER_PROMPT, tools=[get_order_status])
    return str(agent(query))


@tool
def returns_agent(query: str) -> str:
    """Handle RETURN ELIGIBILITY and REFUND questions: whether an item can be
    returned, return windows, restocking fees, refund timing and method. Use for
    anything about sending an item back or getting money back.

    Args:
        query: the customer's return/refund question, verbatim.
    """
    agent = Agent(model=load_model(), system_prompt=RETURNS_PROMPT, tools=[get_return_policy])
    return str(agent(query))

# ---------------------------------------------------------------------------
# Supervisor: routes, or declines when unsure.
# ---------------------------------------------------------------------------

SUPERVISOR_PROMPT = """
You are a customer-support orchestrator for Acme. You do not answer questions
yourself and you do not ask the customer for information you can look up. You
have two specialists available as tools:

- order_agent: order status and shipping (tracking, delivery, "where is my
  order"), and looking up the details of an order by its order ID (including
  which products are in it).
- returns_agent: return eligibility and refunds (whether an item can be sent
  back, return windows, restocking fees, refund rules and timing).

How to handle a message:
1. Judge by the customer's INTENT, not just keywords. A message that mentions an
   order number but asks to send the item back is a RETURNS request.
2. If it clearly fits ONLY orders/shipping, call order_agent and return its answer.
3. If it clearly fits ONLY returns/refunds AND the customer already named the
   product, call returns_agent and return its answer.
4. If it is a returns/refund request that references an ORDER ID but NOT a
   product name, you MUST resolve it in two steps: FIRST call order_agent to look
   up that order and identify the product(s) in it, THEN call returns_agent,
   passing the product name(s) you learned so it can give the return policy. Do
   NOT ask the customer which product it is — look it up yourself.
5. You may call more than one specialist when a request needs information from
   both. Feed the result of the first call into the next call as needed.
6. Only if the message is genuinely ambiguous about intent (could be either an
   order-tracking OR a returns request), or fits neither, DO NOT call any tool.
   Instead reply: "I'm not sure whether this is an order or a returns question.
   Could you clarify, or I can connect you with a support agent." Never guess.

Return the specialist's final answer to the customer.
"""


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
            system_prompt=SUPERVISOR_PROMPT,
            tools=[order_agent, returns_agent, get_product_faq],
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
