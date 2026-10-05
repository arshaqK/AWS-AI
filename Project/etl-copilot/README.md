# ETL Copilot

A multi-agent assistant for data engineers. You upload a raw vendor CSV; the agents profile it,
propose a contract, write a Glue ETL script, run it, validate the result and document it.
**Nothing runs until you approve it.**

Built on Amazon Bedrock AgentCore (Strands agents, Claude Sonnet 4.6), AWS Glue, Athena and S3
in `us-west-2`. Architecture diagram: [docs/architecture.drawio](docs/architecture.drawio).

---

## The workflow

| # | Step | Who | What happens |
|---|------|-----|--------------|
| 1 | **Upload** | You | Drop a CSV in the console. It is checked (UTF-8, comma-separated, unique header) and saved to `landing/<dataset>/`. |
| 2 | **Catalog** | Supervisor | A new dataset gets a raw table: the Glue Crawler runs once (1–3 min). |
| 3 | **Profile** | Spec / Script Writer | Athena reads every row: formats, null spellings, distinct values, units, negatives. Cached until the file changes. |
| 4 | **Contract** | Spec Writer | *New datasets only.* Proposes the contract: columns, types, key, personal-data handling, cleaning and validation rules. |
| 5 | **Approve contract** | **You** | Approve, request changes, or reject. Only your approval installs it. |
| 6 | **Draft ETL** | Script Writer | Writes a Python script against the contract and lists any guesses as *Check before approving*. |
| 7 | **Approve draft** | **You** | Approving one draft allows exactly one run. |
| 8 | **Run** | Execution Agent | Copies the exact approved script to the Glue job and runs it: raw CSV → clean Parquet in `staging/`. |
| 9 | **Validate** | Execution Agent | Checks the table against every contract rule with Athena. On failure the Script Writer revises the draft and you approve again (back to 7). |
| 10 | **Document** | Documenter | Writes the data dictionary, the quality report and a CSV copy of the clean table. |

A dataset that already has a contract skips steps 4–5: one approval per run.

---

## The agents

| Agent | Runs when | Does | Produces |
|---|---|---|---|
| **Supervisor** | Every message | Reads your request, calls the right specialist, never writes specs or code itself | The reply and the next step |
| **Spec Writer** | A dataset has no contract, or you ask to change it | Profiles the data and proposes the contract | Contract proposal (`scripts/contracts/drafts/<id>.json`) |
| **Script Writer** | A contract is approved, you request changes, or a run failed | Writes or revises the ETL script | Draft (`scripts/drafts/<id>.py`) |
| **Execution Agent** | You approve a draft | Starts Glue, waits, validates the output | Run result + rule results |
| **Documenter** | Validation passed | Describes each column; every number comes from code | `data_dictionary.md`, `quality_report.md`, `<dataset>.csv` |

---

## Features

**Human approval gate.** Your `APPROVE` / `REJECT` is read by code *before any model sees the
message*, so no model can approve anything. IDs are hashes of the exact contract or script you
reviewed; a changed file needs a new approval. Approvals live in S3 (`approvals/`), are used once
and expire after 24 hours.

**Contracts and validation rules.** Every dataset is checked by three built-in rules
(schema matches, no rows lost, unique key) plus dataset rules the Spec Writer proposes from the
data: `date_range`, `allowed_values`, `matches` (pattern) and `hashed` (personal data is SHA-256).
A rule must accept the data the profile showed; anything uncertain is asked, not guessed.
Rules run as Athena SQL written by code.

**Self-correcting runs.** A failed job or rule goes back to the Script Writer with the exact error
and offending values. It must fix those values, not loosen the rule, and flag any guess for you.

**Your exact words.** *Request changes* text is passed to the writer word for word (every
comma-separated part); the revised card shows a *What changed* line per item.

**Memory of conventions** (AgentCore Memory). Points you confirm by approving a draft — or with
`REMEMBER <dataset>: <text>` — are remembered per dataset and applied to future contracts and drafts.
`FORGET <dataset> <n>` removes one. Only your commands write memory; personal data is refused.

**Personal data protection.** Contracts hash, drop or redact personal columns. The console never
shows raw emails or phone numbers, and docs are scanned for personal data before saving.

**Least privilege.** Every tool runs as `EtlCopilotExecutionRole`: it can read `landing/` but
never write it, can run only the one Glue job, crawler and Athena workgroup, and cannot touch
approvals or memory. Only the runtime role and your login may assume it.

**Run log and metric.** Every run is recorded from Glue and Athena (not from the agent's words) in
`reports/runs/<dataset>/`. First-run success rate: `uv run python -m tools.runlog`.

**Copilot Console** (Streamlit, localhost). Upload, live narration of each agent step with a status
line ("Script Writer is writing the ETL script…"), approval cards with Approve / Request changes /
Reject, a run-status rail, remembered conventions, and a result card with a CSV download.
Narration is built by code from real results, never from model claims.

---

## AWS resources

| Resource | Name | Purpose |
|---|---|---|
| S3 bucket | `etl-copilot-ak` | `landing/` raw CSV · `staging/` clean Parquet · `scripts/` contracts, drafts, job script · `reports/` docs, run log, CSV · `approvals/` |
| Glue Crawler | `etl-copilot-landing-crawler` | Raw tables `etl_copilot_raw.raw_<dataset>` |
| Glue job | `etl-copilot-job` | Python Shell 3.9, runs the approved script |
| Glue databases | `etl_copilot_raw`, `etl_copilot_clean` | Raw and clean tables |
| Athena workgroup | `etl-copilot-wg` | Profiling, validation, CSV export |
| AgentCore Runtime | `EtlCopilot` | The agents (2 h idle timeout) |
| AgentCore Memory | `EtlCopilotMemory` | Confirmed conventions (365-day retention) |
| IAM roles | `EtlCopilotExecutionRole`, runtime role | Tool access; agent runtime |

---

## Project layout

```
etl-copilot/
├── app/EtlCopilot/        the agent (deployed to AgentCore Runtime)
│   ├── main.py            entrypoint + Supervisor
│   ├── approvals.py       approval gate and change requests (code, not a model)
│   ├── memory.py          remembered conventions
│   ├── narration.py       events streamed to the console
│   ├── agents/            spec_writer, script_writer, execution_agent, documenter
│   ├── tools/             profile, specs, drafts, execution, catalog, docs, export, runlog, pii
│   └── iam/               runtime role policy
├── ui/                    Copilot Console (Streamlit): app.py, backend.py, upload.py, console_view.py
├── agentcore/             AgentCore project config and CDK
├── data/                  sample datasets, generators and answer keys
├── iam/                   Execution role trust policy
├── tests/                 test suites (see below)
└── docs/                  architecture diagram
```

Sample datasets: `orders_export.csv` (508 rows), `inventory_snapshot.csv` (243),
`support_tickets.csv` (405), each with an answer key of the quirks it contains.

---

## Run it

Prerequisites: AWS login for account `481719141347` (us-west-2), `uv`, Node.js 20+, AgentCore CLI.

```bash
# 1. The agent - either run it locally...
agentcore dev --port 8081 --logs --skip-deploy        # from etl-copilot/
# ...or deploy it to AgentCore Runtime
agentcore deploy --target dev

# 2. The console
cd ui && uv run streamlit run app.py                  # http://localhost:8501
```

Pick **Local** or **Deployed** in the console sidebar, upload a CSV, and follow the cards.
Without the console: `agentcore invoke --target dev --session-id <33+ chars> "Onboard orders"`.

---

## Tests

Run from `app/EtlCopilot/` (`ui/` for the console):

| Suite | Checks |
|---|---|
| `test_approval_gate.py` | Only exact commands approve; unapproved, tampered or reused drafts never run |
| `test_specs.py` | Contracts are validated and installed only by approval |
| `test_narration.py` | Console events match AWS; no personal data in any event |
| `test_requests_cache_export.py` | Exact change requests, profile cache, CSV export |
| `test_memory.py` | Only your commands change memory; the tools' role cannot |
| `test_runtime_ready.py`, `test_trust.py` | Durable approvals; runtime and trust policies |
| `test_runlog.py`, `test_documenter.py`, `verify_answer_keys.py` | Run log, docs, answer keys |
| `ui/tests/test_app.py`, `test_mock_app.py` | Console: upload checks, buttons, cards, status |

---

## Known limits

- Rules are fitted to the first file seen; an `allowed_values` rule on an open-ended column can
  fail on next week's data — review rules on the contract card.
- The Spec Writer sees the profile only; it cannot run its own queries.
- CSV only, one dataset per landing folder.
