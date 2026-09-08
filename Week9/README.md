# Bedrock AgentCore — Customer Support Agent (Tasks 1 & 2)

Customer-support agents built on **Amazon Bedrock AgentCore** (Bedrock Agents Classic is closed to new accounts), using code-based Strands agents, an S3 Vectors knowledge base, and a Bedrock guardrail. Region: `us-west-2`.

> Screenshots are referenced as `.png` in the same folder as this README — adjust the path/extension if yours differ.

---

## Task 1 — Single Agent: Customer Support Bot

![Task 1 architecture](screenshots/task1.png)

A single agent with real tool use, a knowledge base, and a content-safety guardrail.

- **Tools:** `get_order_status` (mock order data) and `get_product_faq` (retrieves from the S3 Vectors knowledge base).
- **Knowledge Base:** self-managed unstructured KB on **Amazon S3 Vectors** with Titan Text Embeddings V2, sourced from a mock product FAQ.
- **Guardrail:** PII masking, denied topics (Legal Advice, Competitor Recommendations), and grounding/relevance checks.
- **Model:** Claude Haiku 4.5 on Bedrock (Sonnet was blocked by an org SCP).

### Testing

Clear order-status query routed to `get_order_status`.

![Clear order query](screenshots/Task1_clear_order.png)

Product question answered from the knowledge base via `get_product_faq`.

![Knowledge base retrieval](screenshots/Task1_KB.png)

PII in the prompt is masked in the response.

![PII masking](screenshots/Task1_PII.png)

Denied-topic prompt blocked by the guardrail.

![Denied topics](screenshots/Task1_deined_topics.png)

Deployed agent invoked from the CLI.

![CLI output](screenshots/Task1_CLI_output.png)

---

## Task 2 — Multi-Agent: Supervisor Routing System

![Task 2 architecture](screenshots/task2.png)

Extends Task 1 into a supervisor that routes each message to the right specialist sub-agent, using the Strands "agents as tools" pattern.

- **Supervisor:** reads the message, routes by **intent** (not keywords), and **declines rather than guessing** when intent is genuinely ambiguous.
- **Order sub-agent:** order status and shipping (`get_order_status`).
- **Returns sub-agent:** return eligibility and refunds (`get_return_policy`, new mock data).
- **Orchestration:** for a returns request that gives an order ID but no product, the supervisor chains `order_agent` → `returns_agent` (looks up the product first, then the policy).
- Guardrail from Task 1 is reused across all agents.

### Testing

Ambiguous prompt (order ID, no product) — initial issue: the returns agent stalls asking for the product.

![Ambiguous prompt issue](screenshots/Task2_ambiguous_prompt_issue.png)

After switching the supervisor to an orchestrator — it chains order → returns and resolves the product automatically.

![Ambiguous prompt resolved](screenshots/Task2_ambiguous_prompt_issue_resolved.png)

Second ambiguous test.

![Ambiguous prompt second test](screenshots/Task2_ambiguous_prompt_second_test.png)

Third ambiguous test.

![Ambiguous prompt third test](screenshots/Task2_ambiguous_prompt_third_test.png)

Fourth ambiguous test.

![Ambiguous prompt fourth test](screenshots/Task2_ambiguous_prompt_fourth_test.png)
