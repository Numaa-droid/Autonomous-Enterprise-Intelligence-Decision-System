# AEIDS — Autonomous Enterprise Intelligence & Decision System

Multi-agent system for analysing company documents (annual reports, ESG/BRSR reports). A user uploads documents and asks a complex question. A Supervisor agent breaks the question into typed tasks, specialist agents do the work, a Verification agent checks every claim, and a report is produced in which every claim links to source evidence.

AEIDS provides **decision support**, not final legal, financial or compliance determinations.

## Read these first
- `docs/ARCHITECTURE.md` — full technical blueprint (agents, contracts, schema, APIs, security, evaluation).
- `docs/MVP_PLAN.md` — what we are building **now** (Release 0) and in what order. Scope decisions here override the full blueprint.
- `docs/DECISIONS.md` — decision log. Add an entry whenever a design or library choice is made.
- `README.md` — how to run AEIDS. `tablelens/README.md` — the TableLens parsing pipeline (setup, models, files).

## Current state of the repo
This repo holds AEIDS (`aeids/`) and, since D-020, a copy of the PDF parsing pipeline **TableLens** in `tablelens/` (copied unchanged from `../IntelligencePDF`, GitHub `PDF-Document-Parser`; edit TableLens here from now on). TableLens runs as its own process with its own env; AEIDS talks to it over HTTP (D-019) and never imports its code.

TableLens files (in `tablelens/`, flat Python files; run them from inside `tablelens/`):

| File | Role |
|---|---|
| `app.py` | FastAPI web UI + detection job (Table Transformer + docling layout fusion, charts, reading order) |
| `content.py`, `structure_tatr.py` | Recognition: table grids (fine-tuned Table Transformer in `models/`, Git LFS), charts and scans (PaddleOCR-VL via llama-server) |
| `reference.py` | Recognition result → reference JSON (`medical-report/1.0`), every value with page and box (PDF points) |
| `medical.py` | Lab-report reader: test / result / unit / range / flag per row |
| `gpu_server.py` | Modal cloud-GPU deployment of the pipeline |

How it relates to AEIDS:
- TableLens is the candidate **parsing / table-extraction layer** for AEIDS ingestion (D-006). Its boxes and page numbers feed `evidence_records`.
- **Medical is not part of AEIDS** (D-010). `medical.py` and the medical reference JSON are a separate TableLens feature: leave them as they are and never import them from `aeids/`. AEIDS ingestion reads the generic recognition output (`output/<name>/structure/<name>_structure.json`) and does its own financial normalisation (units, scale, periods, negatives, totals), ported from `reference.py` as of commit `581640e` (`git show 581640e:reference.py`).
- AEIDS code lives in the `aeids/` package of this repo. Do not change `tablelens/` as part of AEIDS work; if AEIDS needs a change there, raise it with the user. TableLens self-checks: `python app.py` runs them (they pass); `python test_recognise.py` is the fast check, but it is stale and fails in the original too (its stub page has no `get_texttrace`).
- TableLens env: `cd tablelens && uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt` (the old one is `../IntelligencePDF/.venv-tablelens`). Its `doc/` and `output/` are git-ignored; the fine-tuned model is in Git LFS (`.gitattributes`).
- `git show 581640e:reference.py` must be run inside `../IntelligencePDF` (the history was not copied).
- Heavy dependencies (torch, paddle, transformers) are pinned for **Python 3.11**; recognition wants a GPU (local or Modal).
- Known gap: `reference.py` validates against `schema/medical-report.schema.json`, which is not in the repo, so that check is skipped.

## Running AEIDS
```bash
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env                                   # set AEIDS_DB_PASSWORD (and the same in AEIDS_DATABASE_URL)
docker compose up -d                                   # Postgres, Qdrant, Redis on 127.0.0.1 (colima on this Mac)
.venv/bin/alembic upgrade head
.venv/bin/uvicorn aeids.api.app:create_app --factory --port 8001
./start.sh                                             # or all in one: services, migrations, TableLens, API, worker (aeids/start.py)
.venv/bin/pytest                                       # SQLite; AEIDS_TEST_DATABASE_URL=<postgres>/aeids_test for Postgres
```
Status: M1 (foundation) and M2 (ingestion and retrieval) done. Upload → TableLens parse → financial normalisation → section-aware chunks (D-017, D-018) → embeddings + Qdrant index → version becomes current only after its index is verified (D-021) → hybrid search `POST /api/v1/search` (dense + Postgres full-text, RRF; tenant and current-version re-checked in Postgres). Retrieval benchmark: `.venv/bin/python eval/run_retrieval.py` (31 questions on the 5-company corpus, `eval` tenant; repeatable since D-023: pinned provider `nebius`, cached question vectors; hybrid R@5 0.77, R@10 0.90, MRR 0.53). Tracing and console (D-022): every job, search and model call is a trace in `trace_spans`; the web console at `http://127.0.0.1:8001/ui` shows overview, documents, search, evaluation, traces (waterfall) and logs (`logs/*.log`, written by `./start.sh`). M3 done (D-024): typed contracts (`aeids/contracts/`), task/workflow state machines (`aeids/orchestration/states.py`), Supervisor (`supervisor.py`, idempotent `tick`), workers with leases and fencing (`runner.py`, `jobs.py`; workers resume unfinished workflows on start), stub agents (`aeids/agents/stubs.py`: real search + evidence quotes, real structural verification), `POST /api/v1/workflows` and the console Workflows page. M4 done (D-025): calc module (`aeids/calc/`), LLM Document Intelligence (`aeids/agents/document_intelligence/`: maps named companies to uploaded ones, refuses others; the model names table cells, code reads value/unit/period/page/box from the normalised table) and Financial Analyst (`aeids/agents/financial_analyst/`: the model picks calc functions and fact ids, `aeids/calc` computes); `AEIDS_AGENT_MODE` auto/llm/stub (`aeids/agents/registry.py`); tests use a scripted fake model (`tests/test_agents_m4.py`). Metric knowledge base (D-026): `aeids/knowledge/metrics.yaml` (line items with US GAAP / Ind AS / BRSR names; metrics as recipes of `aeids.calc` steps, incl. BRSR intensities and CSR); the loader refuses a bad file; bump its `version` when a definition changes; agents use prompts `plan_v2`/`extract_v2`/`prompt_v2`. M5 done (D-027): deterministic Verification agent (`aeids/agents/verification/`: replays every calculation, checks each value against its table cell, page, unit, currency, scale, period and current version), at most 2 rework rounds then rejected findings are dropped, human review `POST /api/v1/workflows/{id}/reviews` (approve/reject/rerun) and a console panel; fault-injection tests in `tests/test_verification_m5.py`. M6 done (D-029): single-agent RAG baseline (`aeids/baseline/`, `python -m aeids.baseline "q" --tenant eval`), scoring (`aeids/evaluation/scoring.py`), 36 questions (`eval/questions.jsonl`), `.venv/bin/python eval/run_benchmark.py` writes `reports/benchmark-*.md` (AEIDS 92% vs baseline 69%, 0% vs 12% unsupported numbers). Speed (D-030): reasoning off (`AEIDS_LLM_REASONING`), code picks clear table cells from knowledge-base names (`document_intelligence/resolver.py`, `AEIDS_DI_CELL_RESOLVER`), focused planning catalog, analyst/writer model calls skipped when not needed, batched query embeddings; median 4.6 s, USD 0.0004 per question. M7 done (D-031): `aeids/reporting/report.py` builds a `report/1.0` document from verified findings, `check` refuses anything unverified (failed check → workflow FAILED), stored in `reports` (migration 0007) as JSON + Markdown (document text escaped); `GET /api/v1/workflows/{id}/report` and `/report.md`, console download button, `python -m aeids.reporting <id> --tenant eval`; tests `tests/test_report_m7.py`. Release 0 is built; next is a Release 0 review before any Release 1 agents. Tests pass on SQLite and Postgres 18 (in-memory Qdrant, `HashEmbedder`). Corpus: `../IntelligencePDF/doc/*.pdf` and `../IntelligencePDF/output/<name>/structure/` (not copied into `tablelens/`; new parses land in `tablelens/output/`) (AAPL, MSFT, NVDA, TSLA, GOOG; `tests/test_tablelens_sample.py` checks Apple; `eval/corpus.csv` has their checksums).

Automatic parsing (D-019): an upload queues an RQ job that sends the PDF to the TableLens server (`cd tablelens && .venv/bin/python app.py`, port 8000; it uses the Modal GPU server when deployed) chunks the `_structure.json` it returns, then indexes the chunks for search (a version already parsed is only indexed). Run `.venv/bin/python -m aeids.worker` next to the API; `python -m aeids.worker status` checks TableLens, the queue and version states; `python -m aeids.worker requeue` re-queues stuck versions; `POST /api/v1/documents/{id}/versions/{vid}/process` re-runs one. Tests use a fake TableLens (`tests/test_tablelens.py`) and `jobs_mode="off"` by default.

Models (OpenRouter, `AEIDS_OPENROUTER_API_KEY` in `.env`): embeddings `qwen/qwen3-embedding-8b`, LLM `qwen/qwen3.8-flash`. All calls go through `aeids/llm/openrouter.py` (bounded retries, usage/cost/latency recorded); check with `.venv/bin/python -m aeids.llm check`. Tests never call the network (fake transport, `HashEmbedder`).

The user does not want commits or pushes unless they ask.

```bash
.venv/bin/python -m aeids.ingestion inspect ../IntelligencePDF/output/<name>/structure/<name>_structure.json
.venv/bin/python -m aeids.ingestion attach <document_id> <version_id> <path to _structure.json>
```

## Current scope: Release 0 (MVP)
Build only:
1. Data foundation: upload, document versioning, parsing, chunking, embeddings, hybrid retrieval.
2. Supervisor + typed task/result contracts + persisted task and workflow state machines.
3. Three agents: **Document Intelligence**, **Financial Analyst**, **Verification**.
4. Single-agent RAG **baseline** + benchmark harness.
5. Simple report output (Markdown/JSON).

Never in AEIDS scope: medical / lab reports (separate TableLens feature, D-010).

Out of scope until Release 0 is benchmarked: Research, Regulatory Intelligence and Risk Analysis agents, Kubernetes, full multi-tenant auth, frontend dashboard, PDF report styling. Keep `tenant_id` in the schema and filters from day one, but a single default tenant is fine.

## Non-negotiable rules
1. **Evidence for every claim.** Every finding links to `evidence_records` (document version, page, section, bounding box where available, quote). No evidence → not a finding.
2. **No LLM arithmetic.** All numbers are computed by the deterministic calculation module (`aeids/calc/`). Store function, formula, typed inputs (value, unit, scale, currency, period), output and `calc_version` for every calculation in `calculations`.
3. **Typed contracts only.** Agents exchange Pydantic-validated `TaskMessage` / `AgentResult` objects (versioned schemas). No free-form agent-to-agent chat. Agents never call each other directly.
4. **Only the Supervisor changes task and workflow state** (workers only claim a task and release it on lease expiry). Agents submit results and a proposed `next_action`.
5. **Persist every state transition** in PostgreSQL. Only transitions listed in `docs/ARCHITECTURE.md` §3.1 are legal; anything else raises. Workflows must resume after a worker restart without redoing completed tasks (unique idempotency key per task).
6. **Explicit document versions.** Default retrieval uses only `is_current = true`. On replacement: keep old version, index new version, verify indexing, then switch the current pointer.
7. **Uploaded and retrieved text is untrusted data.** Never let document content act as instructions or trigger tools.
8. **Bounded retries** with exponential backoff for transient errors only. Never retry permission errors; retry schema-validation failures at most once.
9. **Secrets** come from environment variables. Never put keys in code, prompts, logs or reports.
10. Every vector query filters by `tenant_id` (and `access_scope` when used).
11. **No documents or outputs in git.** Input PDFs (`doc/`, `*.pdf`) and results (`output/`) stay git-ignored; benchmark corpus files are referenced by URL and checksum. Personal data in documents (e.g. patient names and ids in medical reports) is redacted by default, as `reference.py` does.

## Stack (see docs/DECISIONS.md for open choices)
- Python 3.11, FastAPI, Pydantic v2
- PostgreSQL (source of truth) with SQLAlchemy 2.x + Alembic migrations
- Qdrant (vector index) — payload includes tenant, company, document, version, page range, section, `is_current`
- Local filesystem or MinIO for original files (no large binaries in Postgres)
- Redis + background worker for ingestion and async tasks
- Parsing: the TableLens pipeline (proposed, D-006)
- Langfuse or OpenTelemetry for traces
- pytest for tests; Docker Compose for local services

## Suggested repo layout
```
aeids/
  api/            # FastAPI app factory, routers (documents, workflows), response schemas
  db/             # SQLAlchemy models, Alembic migrations (db/migrations/versions/)
  documents.py    # upload validation, checksum dedupe, versioning
  storage.py      # ObjectStore; LocalStore (D-005)
  ingestion/      # reads TableLens output; normalisation, chunking, embedding, indexing
  retrieval/      # hybrid search with tenant/version filters
  contracts/      # Pydantic TaskMessage, AgentResult, Finding, Evidence (versioned)
  orchestration/  # Supervisor, agent registry, state machines, retries
  agents/         # document_intelligence, financial_analyst, verification
  calc/           # deterministic calculation library (pure functions)
  reporting/      # report assembly from verified findings only
  baseline/       # single-agent RAG baseline
eval/
  corpus.csv      # benchmark documents: source URL, fiscal year, checksum
  questions.jsonl # ground-truth benchmark
  run_eval.py
tests/
docs/
requirements.txt, docker-compose.yml, alembic.ini, .env.example
```

## Coding conventions
- Type hints everywhere; Pydantic models for all external and inter-agent data.
- Use `Decimal` for financial values; never float for money. In JSON, decimals are serialised as strings (`"12.5"`).
- Keep unit, scale and currency as separate fields (`unit: "currency"`, `currency: "INR"`, `scale: "crore"`), never fused into one string.
- Prompts live in versioned files (e.g. `agents/<name>/prompt_v1.md`); store `prompt_version` in `agent_runs`.
- Every agent run records: model, prompt version, trace ID, token usage, latency, error.
- Write tests alongside code. The calc module and state machines need thorough unit tests.
- Small, focused commits. Update `docs/DECISIONS.md` when making a design choice.

## Definition of done for Release 0
- Upload 3–5 company reports; see per-version processing status.
- A financial question produces a visible task plan; Supervisor respects dependencies.
- Agents return schema-valid outputs with evidence IDs.
- Calculations are deterministic and reproducible from stored inputs.
- Verification rejects injected wrong numbers/citations and triggers bounded rework.
- Report contains only verified findings, with citations and limitations.
- Workflow resumes after worker restart without duplicating completed work.
- Benchmark runs repeatably and compares AEIDS against the single-agent RAG baseline.
