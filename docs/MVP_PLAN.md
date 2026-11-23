# AEIDS — Release 0 (MVP) Build Plan

This plan narrows `ARCHITECTURE.md` to a version that can be finished, measured and shown. Where it conflicts with the full blueprint, **this file wins** for Release 0.

## 1. Scope

**In scope**
- Upload + explicit document versioning + processing status
- Parsing and table extraction through the existing TableLens pipeline (`app.py`, `content.py`; D-006), chunking with page/section metadata, evidence boxes kept from the parser
- Qdrant indexing + hybrid retrieval (dense + keyword) with tenant/version filters
- Pydantic contracts, agent registry, persisted task and workflow state machines, retries, idempotency, resume-after-restart
- Agents: **Supervisor**, **Document Intelligence**, **Financial Analyst**, **Verification**
- Deterministic calculation module
- Single-agent RAG baseline + benchmark harness
- Markdown/JSON report built only from verified findings

**Deferred (Release 1+)**
- Research, Regulatory Intelligence, Risk Analysis agents
- Frontend dashboard, styled PDF reports, charts
- Full authentication/RBAC and real multi-tenancy (keep `tenant_id` columns and filters; use one default tenant)
- Human review UI (store `NEEDS_HUMAN_REVIEW` status; review via API is enough)
- Kubernetes, production hardening, cost controls beyond basic token logging

## 2. Build order (milestones)

Build vertically: get one question flowing end to end before polishing any single layer. Each milestone is finished when its "done when" holds; the next one starts after that.

### M1 — Foundation
- Docker Compose: Postgres, Qdrant, Redis (+ MinIO if used), next to the existing TableLens service
- FastAPI skeleton for `aeids/api/`, config via env vars, health endpoint
- SQLAlchemy models + Alembic migration: `tenants`, `companies`, `documents`, `document_versions`, `document_chunks`, `evidence_records`
- `POST /api/v1/documents`, `POST /api/v1/documents/{id}/versions`, `GET /api/v1/documents/{id}/versions`
- SHA-256 checksum on upload; duplicate upload of same checksum returns existing version
- Versioning rule: keep old → index new → verify → switch `is_current`. Until indexing existed (M2), a stored version became current directly; since M2 the full rule applies (D-021).
- **Done when:** a PDF can be uploaded twice as two versions, and only one is current.

### M2 — Ingestion and retrieval
- Background ingestion job: TableLens detection + recognition → `_structure.json` → financial normalisation (units, scale, periods, negatives, totals; ported from `reference.py` at commit `581640e`, D-010) → chunk (page_start/page_end/section) → embed → upsert to Qdrant with full payload
- Evidence boxes (PDF points) and page numbers from the parser are stored with each chunk
- `parser_status` transitions persisted on `document_versions`
- Hybrid retrieval function with mandatory `tenant_id` and `is_current` filters
- Retrieval test set (≥20 questions with known supporting pages); measure Recall@K and MRR
- **Done when:** retrieval eval runs from one command and reports numbers.

### M3 — Contracts and Supervisor
- Pydantic v2 models: `TaskMessage`, `AgentResult`, `Finding`, `Quantity`, `Calculation`, `EvidenceRef` with `schema_version`
- Tables: `workflows`, `agent_tasks`, `task_dependencies`, `task_transitions`, `agent_runs`, `findings`, `finding_evidence`, `calculations`, `verification_results`
- Task and workflow state machines with the transitions in `ARCHITECTURE.md` §3.1 only (illegal transition → exception)
- Agent registry; Supervisor produces a task graph with dependencies and acceptance criteria
- Agents may be **stubs** returning fixed valid output at this stage
- Retry with exponential backoff; unique idempotency key per task; worker lease so a restart re-queues `RUNNING` tasks and never redoes `SUCCEEDED` or `VERIFIED` ones
- `POST /api/v1/workflows`, `GET /api/v1/workflows/{id}`, `GET /api/v1/workflows/{id}/tasks`
- **Done when:** a stub workflow survives a killed worker and finishes without duplicate work.

### M4 — Document Intelligence + Financial Analyst
- Document Intelligence: extracts requested metrics from retrieved chunks/tables; every fact gets an `evidence_record` (version, page, section, bbox, quote); never infers missing values
- Calculation module (see §4) with full unit tests
- Financial Analyst: chooses which calc functions to call; the LLM never computes numbers; findings carry a stored calculation (function, formula, typed inputs, output) and evidence IDs
- **Done when:** "Compare revenue and operating margin FY2023–FY2025" returns schema-valid findings with evidence.
- **Status:** done (D-025; metric knowledge base D-026). Shown live on Apple's 10-K: 6 reported values and 5 calculations, all verified; `tests/test_agents_m4.py`, `tests/test_calc.py`.

### M5 — Verification + rework loop
- Replays every calculation from `calculations` and compares output
- Resolves each evidence ID and checks the quoted value, page, version, unit and period match the claim
- Claim-level decision: `verified` / `rejected` / `needs_review` with reasons, stored in `verification_results`
- `rejected` → Supervisor creates a rework task (bounded: max 2 rework rounds; see `ARCHITECTURE.md` §3.1 Rework)
- Fault-injection tests: wrong number, wrong page, superseded version, unit mismatch, scale mismatch, wrong period
- **Done when:** all injected faults are caught in tests.
- **Status:** done (D-027). `tests/test_verification_m5.py`: every injected fault is rejected with its reason and reworked; human review endpoint and console panel.

### M6 — Baseline and benchmark
- Single-agent RAG baseline: same retrieval, same LLM, one prompt, answer with citations
- Run both systems on `eval/questions.jsonl`
- Report: answer accuracy, citation correctness, unsupported-claim rate, latency, tokens/cost per question
- **Done when:** one command produces a comparison table.
- **Status:** done (D-029; speed and token work D-030). `.venv/bin/python eval/run_benchmark.py` on 36 questions: AEIDS 92% vs baseline 69% correct, 0% vs 12% unsupported numbers, median 4.6 s vs 2.0 s, USD 0.0004 vs 0.0002 per question; `tests/test_baseline_m6.py`, `tests/test_speed_d030.py`.

### M7 — Report and polish
- Report generator using only `verified` findings; includes citations, limitations, review status
- `GET /api/v1/workflows/{id}/findings`, `GET /api/v1/workflows/{id}/report`
- README with architecture diagram, benchmark table, demo instructions (keep the TableLens setup section)
- Only then consider Release 1 agents

## 3. Agent status → task state mapping (proposed)

All transitions below are rows of the task table in `ARCHITECTURE.md` §3.1.

| Agent result `status` | Next task state | Supervisor action |
|---|---|---|
| `completed` with findings | `SUCCEEDED` → `VERIFYING` | Send findings to Verification |
| `completed` without findings | `SUCCEEDED` → `VERIFIED` | Nothing to verify; release dependents |
| `needs_more_information` | `BLOCKED` | Create a dependency task if a registered agent can supply it (`BLOCKED` → `QUEUED` once it is `VERIFIED`); otherwise `NEEDS_HUMAN_REVIEW` |
| `blocked` | `BLOCKED` | Record reason; escalate to `NEEDS_HUMAN_REVIEW` if not resolvable |
| `retryable_error` | `FAILED_RETRYABLE` → `QUEUED` | Retry with backoff while `attempts < max_attempts`; then `FAILED_FINAL` |
| `final_error` | `FAILED_FINAL` | Dependents `PLANNED` → `BLOCKED`; workflow continues with what it can |
| *(output fails schema validation)* | `FAILED_RETRYABLE` once, then `FAILED_FINAL` | Retry once with the validation error in context |
| *(permission error)* | `FAILED_FINAL` | Never retry |
| *(worker lost, lease expired)* | `RUNNING` → `QUEUED` | Counts as an attempt; same idempotency key |

Verification outcomes per task:

| Verification result | Next task state |
|---|---|
| all material claims `verified` | `VERIFIED` |
| any claim `rejected`, rework rounds remaining | `REJECTED`; rework task created, dependents re-pointed to it |
| any claim `rejected`, no rounds left | rejected claims dropped or qualified; task `VERIFIED` with warnings, or `NEEDS_HUMAN_REVIEW` if material |
| any claim `needs_review` | `NEEDS_HUMAN_REVIEW` |

Human review (`POST /api/v1/workflows/{id}/reviews`): approve → `VERIFIED`, re-run → `QUEUED`, reject → `FAILED_FINAL`.

## 4. Calculation module spec (`aeids/calc/`)

- Pure functions, no I/O, no LLM. Inputs and outputs use `Decimal`; JSON stores them as strings.
- Every input is a `Quantity`, and every function returns a `CalcResult`:
  ```python
  class Quantity(BaseModel):
      value: Decimal
      unit: str               # "currency", "percent", "ratio", "count", ...
      scale: str = "units"    # units / thousand / lakh / million / crore / billion
      currency: str | None    # ISO 4217, e.g. "INR"; required when unit == "currency"
      period: str | None      # e.g. "FY2025"
      evidence_id: str | None # the source value's evidence record

  class CalcResult(BaseModel):
      function: str           # e.g. "pct_change"
      formula: str            # human-readable formula
      inputs: dict[str, Quantity]
      output: Quantity        # full precision
      calc_version: str       # bump when logic changes
  ```
- Initial functions: `normalize_scale`, `pct_change`, `ratio`, `margin`, `cagr`, `sum_values`, `difference`.
- Refuse (raise a typed error) on: currency mismatch, scale mismatch after failed normalization, period mismatch where not allowed, division by zero, missing input.
- Rounding happens only at presentation time; store full-precision outputs.
- Tests: hand-computed cases, scale conversions (lakh ↔ crore ↔ million), edge cases, and property tests (e.g. `pct_change(a, a) == 0`).

## 5. Corpus and ground truth

**Corpus:** 3–5 public company annual reports (+ BRSR where available) for FY2023–FY2025. Use only public documents, no employer data. Record each file's source URL, fiscal year and checksum in `eval/corpus.csv`; the PDFs themselves stay out of git (`doc/` is git-ignored).

**Benchmark:** 30–50 questions in `eval/questions.jsonl`, mixing:
- direct lookups ("What was FY2024 consolidated revenue?")
- calculations ("Revenue growth FY2024→FY2025?")
- multi-document comparisons
- trick questions whose answer is not in the documents (expected: "not found")

Record format:
```json
{
  "id": "q001",
  "company": "<company>",
  "question": "What was consolidated revenue growth from FY2024 to FY2025?",
  "type": "calculation",
  "expected_value": "12.5",
  "unit": "percent",
  "tolerance": "0.1",
  "supporting_evidence": [
    {"document": "<file>", "fiscal_year": 2025, "page": 45},
    {"document": "<file>", "fiscal_year": 2024, "page": 52}
  ],
  "notes": ""
}
```

**Metrics:** answer accuracy (within tolerance), citation correctness (cited page actually supports the claim), unsupported-claim rate, correct "not found" rate, latency, tokens and cost per question.

## 6. Release 0 checklist
- [ ] Multiple reports uploaded; versions and processing status visible
- [ ] Financial question → visible task plan with dependencies
- [ ] Schema-valid agent outputs with evidence IDs
- [ ] Deterministic, reproducible calculations with tests
- [ ] Verification catches all injected faults; bounded rework works
- [ ] Report contains only verified findings, citations and limitations
- [ ] Resume after worker restart without duplicated work
- [ ] Benchmark vs single-agent baseline runs from one command
- [ ] README with diagram, benchmark table and demo
