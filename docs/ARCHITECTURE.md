# AEIDS — Technical Design Blueprint

System architecture • 8-agent design • Prompt foundations • Database schema • Communication protocol

> This is the full target design. For what is being built **now**, see `MVP_PLAN.md`; its scope decisions take priority.

## 0. Purpose and architecture decisions

AEIDS is an implementation-oriented design covering system architecture, eight agent responsibilities and prompt foundations, typed communication contracts, database schema, APIs, governance, evaluation and a phased build plan.

**Approach:** eight core agents coordinated by one Supervisor. OCR, parsing, embeddings, retrieval, calculations, queues and persistence are **deterministic services, not LLM agents**. The platform provides decision support, not final legal, financial or compliance determinations.

- Initial domains: financial performance, ESG/regulatory disclosure mapping, enterprise risk intelligence.
- Every material finding links to source evidence, document version and page/section where available.
- Financial calculations are performed by deterministic, tested code that retains formulas and inputs.
- Agents communicate through typed task/result contracts, not free-form chat.
- Least-privilege tools, execution limits, audit logs, retries and human approval for consequential actions.
- Document versions are explicit so superseded content cannot contaminate current retrieval.

## 1. System architecture

```
USER / FRONTEND (React or Next.js dashboard)
        |
        v
FASTAPI APPLICATION
  Authentication | Upload API | Query API | Report API
        |
        +-------------------------------+
        |                               |
        v                               v
DOCUMENT PIPELINE                 SUPERVISOR AGENT
  File validation                   Plan / route / monitor
  OCR / layout / tables                 |
  Chunk / metadata / embeddings         v
        |                         ORCHESTRATION ENGINE
        v                           Task graph / state machine
QDRANT VECTOR DB                        |
PostgreSQL metadata                     v
                                  SHARED TASK CONTEXT
                                    Task IDs / outputs / evidence / status
                                        |
     +-------------+-------------+------+------+-------------+
     |             |             |             |             |
  Document      Research     Financial    Regulatory       Risk
   Agent         Agent        Analyst       Agent          Agent
     |             |             |             |             |
     +-------------+-------------+------+------+-------------+
                                        |
                               VERIFICATION AGENT
                               Accept / reject / revise
                                        |
                               REPORT GENERATION
                                        |
                               PDF / dashboard / API
```

**Cross-cutting components**
| Component | Role |
|---|---|
| PostgreSQL | Workflow + metadata; source of truth for users, documents, versions, tasks, agent runs, findings, reports |
| Qdrant | Vector retrieval; chunk embeddings with tenant_id, company_id, document_id, document_version_id, access-scope metadata |
| Redis / worker queue | Async jobs (ingestion, background tasks) |
| Object storage | Original files and derived artifacts (no large binaries in Postgres) |
| Langfuse / OpenTelemetry | Traces |
| Human review | Approval gates |

- Persist every workflow state transition.
- MCP is an optional standardized tool interface. It is **not** the orchestrator and does not replace typed internal contracts.

## 2. Eight-agent specification

Each agent has a narrow role, explicit tools, typed inputs/outputs, bounded retries and a measurable completion condition. The Supervisor selects only the agents a request needs.

### 2.1 Supervisor / Orchestrator
- **Purpose:** Owns the task plan and workflow state. Classifies requests, decomposes work, assigns dependencies, selects agents, monitors execution, handles failures, decides when verification/reporting can proceed.
- **Inputs:** user objective, tenant context, document IDs, policy constraints, task history.
- **Tools:** planner, agent registry, workflow state API, task queue, status API. No direct source-document editing.
- **Output:** task graph with task IDs, assigned agents, dependencies, priorities, acceptance criteria, deadlines, retry policy.
- **Prompt foundation:** You are the AEIDS Supervisor. Convert the objective into the smallest sufficient set of verifiable tasks. Select registered agents only. Declare dependencies and acceptance criteria. Never fabricate evidence. Persist state transitions. Escalate ambiguity, missing permissions or consequential actions.
- **Completion:** required tasks are completed, blocked with reason, or escalated; verification gates are resolved.

### 2.2 Document Intelligence
- **Purpose:** Coordinates extraction from uploaded documents. Deterministic services perform parsing, OCR, layout, table extraction, chunking and indexing.
- **Inputs:** document IDs/versions, page ranges, extraction profile, requested fields.
- **Tools:** parser, OCR/layout service, table extractor, metadata service, chunking and indexing services.
- **Output:** normalized entities/metrics, table references, page/section provenance, extraction warnings, indexing status.
- **Prompt foundation:** Use approved extraction tools. Preserve exact values, units, currencies, dates, headers and source locations. Never infer missing values. Flag uncertain or conflicting extraction results. Return structured facts and warnings.
- **Completion:** requested documents/pages have extraction status and every emitted fact has provenance.

### 2.3 Research Agent
- **Purpose:** Finds external information for industry context or current regulatory/background research, preserving provenance and dates.
- **Inputs:** research question, company/industry context, jurisdiction, time window, allowed domains.
- **Tools:** approved search, source fetcher, source allowlist, date/provenance parser.
- **Output:** sourced notes, URL, publisher, publication/retrieval dates, relevance rationale, limitations.
- **Prompt foundation:** Research only the assigned question. Prefer authoritative primary sources. Record title, publisher, URL and dates. Separate facts from interpretation. Flag stale or conflicting evidence. Do not rely on snippets for material claims.
- **Completion:** material external claims have retrievable source records or are marked unsupported.

### 2.4 Financial Analyst
- **Purpose:** Analyzes financial performance using normalized source facts and deterministic calculations.
- **Inputs:** extracted statements/KPIs, periods, currency, company context, analysis question.
- **Tools:** calculation library, ratio calculator, currency/unit normalizer, time-series comparison.
- **Output:** metrics, formulas, inputs, period comparisons, observations, caveats, evidence IDs.
- **Prompt foundation:** Use calculation tools for arithmetic and ratios. State formulas, inputs, units, periods and sources. Distinguish reported from calculated values. Do not compare unlike currencies or periods without normalization and caveats. Flag missing/inconsistent data.
- **Completion:** every calculation is reproducible and each source figure has provenance.

### 2.5 Regulatory Intelligence
- **Purpose:** Maps company disclosures to applicable requirements, considering jurisdiction, entity scope, reporting period and regulation version.
- **Inputs:** jurisdiction, entity facts, reporting period, disclosures, applicable regulation corpus.
- **Tools:** regulatory retrieval, requirement registry, mapping service, evidence matcher.
- **Output:** requirement-to-disclosure mappings with status `evidenced` / `partial` / `not_found` / `not_applicable` / `needs_review`, plus rationale and evidence.
- **Prompt foundation:** Use authoritative, versioned requirements. Map each requirement to exact disclosure evidence. Absence in searched material is not proof of non-compliance. Distinguish not found, insufficient evidence, not applicable and potential gap. Escalate legal uncertainty.
- **Completion:** each in-scope requirement has status, rationale, version and evidence or an explicit gap.

### 2.6 Risk Analysis
- **Purpose:** Synthesizes financial, operational, ESG and regulatory findings into a structured risk register.
- **Inputs:** financial findings, regulatory mappings, research evidence, company context, approved taxonomy.
- **Tools:** risk taxonomy, scoring rules, anomaly service, evidence lookup.
- **Output:** risk statements, category, likelihood/impact rationale, severity methodology, evidence, uncertainty, review action.
- **Prompt foundation:** Identify risks only when evidence supports them, or label them as hypotheses. Apply approved scoring consistently. Explain likelihood and impact. Do not present scores as certainty or make final business decisions.
- **Completion:** each risk has evidence, rationale, category, scoring method/version and uncertainty.

### 2.7 Verification
- **Purpose:** Independently checks claims, citations, calculations, mappings and contradictions; rejects unsupported findings and requests targeted rework.
- **Inputs:** candidate claims, source evidence, calculation traces, regulation versions, acceptance criteria.
- **Tools:** source retrieval, citation resolver, calculation replay, schema validator, contradiction checker.
- **Output:** claim-level `verified` / `rejected` / `needs_review`, reasons, corrected references, rework instructions.
- **Prompt foundation:** Verify every material claim against cited sources and task criteria. Recalculate numeric claims from stored inputs. Check source version, page, units and context. Reject unsupported claims. Return claim-level decisions.
- **Completion:** all material claims have a status; unresolved claims are removed, qualified or escalated.

### 2.8 Report Generation
- **Purpose:** Turns approved findings into an executive report without introducing new factual claims.
- **Inputs:** verified findings, citations, risk register, limitations, report template, audience.
- **Tools:** report renderer, chart builder, PDF exporter, citation formatter.
- **Output:** report artifact, structured report JSON, citation index, generation metadata.
- **Prompt foundation:** Use only approved verified findings. Preserve qualifications, units, periods, references and uncertainty. Separate facts, analysis, risks and recommendations. Do not add claims or imply assurance. Include scope and limitations.
- **Completion:** report passes schema, citation, completeness and formatting checks.

## 3. Agent communication protocol

Supervisor-led task graph with typed messages. Agents do not invoke one another. The Supervisor schedules tasks; agents read and write through approved task and evidence services. This keeps execution observable, replayable and permission-controlled.

### 3.1 Task and workflow lifecycles

Task states and workflow states are separate state machines. Only the transitions listed here are legal; any other transition raises an error and is not persisted.

**Task transitions**

| From | To | When |
|---|---|---|
| `CREATED` | `PLANNED` | Supervisor adds the task to the plan with its dependencies |
| `PLANNED` | `QUEUED` | every dependency is `VERIFIED` |
| `PLANNED` | `BLOCKED` | a dependency ended `FAILED_FINAL`, `REJECTED` without rework, or `CANCELLED` |
| `QUEUED` | `RUNNING` | a worker claims the task (by idempotency key; one claim at a time) |
| `RUNNING` | `SUCCEEDED` / `FAILED_RETRYABLE` / `FAILED_FINAL` / `BLOCKED` / `NEEDS_HUMAN_REVIEW` | agent result (mapping in `MVP_PLAN.md` §3) |
| `RUNNING` | `QUEUED` | the worker's lease expired (crash/restart); counts as an attempt |
| `FAILED_RETRYABLE` | `QUEUED` | after backoff, while `attempts < max_attempts` |
| `FAILED_RETRYABLE` | `FAILED_FINAL` | attempts exhausted |
| `SUCCEEDED` | `VERIFYING` | the result has findings |
| `SUCCEEDED` | `VERIFIED` | the result has no findings (nothing to verify) |
| `VERIFYING` | `VERIFIED` / `REJECTED` / `NEEDS_HUMAN_REVIEW` | verification outcome (`MVP_PLAN.md` §3) |
| `BLOCKED` | `QUEUED` | the Supervisor resolved the cause (e.g. a new dependency task is `VERIFIED`) |
| `BLOCKED` | `NEEDS_HUMAN_REVIEW` / `FAILED_FINAL` | not resolvable by a registered agent |
| `NEEDS_HUMAN_REVIEW` | `QUEUED` / `VERIFIED` / `FAILED_FINAL` | reviewer asks for a re-run / approves / rejects |
| any non-terminal | `CANCELLED` | the workflow is cancelled |

Terminal task states: `VERIFIED`, `REJECTED`, `FAILED_FINAL`, `CANCELLED`.

**Rework:** a `REJECTED` task is never re-run. The Supervisor creates a rework task with `parent_task_id` = the rejected task and `rework_round` + 1, carrying the verifier's rework instructions. Tasks that depended on the rejected task now depend on the rework task. When no rework rounds are left, dependents become `BLOCKED`.

**Workflow transitions**

| From | To | When |
|---|---|---|
| `CREATED` | `PLANNING` | workflow accepted |
| `PLANNING` | `RUNNING` | task graph persisted |
| `RUNNING` | `NEEDS_HUMAN_REVIEW` | a task waits on a reviewer and nothing else can run |
| `NEEDS_HUMAN_REVIEW` | `RUNNING` | review submitted |
| `RUNNING` | `REPORTING` | every task is terminal |
| `REPORTING` | `COMPLETED` | report built from verified findings (partial results are listed as limitations) |
| `PLANNING` / `RUNNING` / `REPORTING` | `FAILED` | planning or reporting failed, or no task can make progress |
| any non-terminal | `CANCELLED` | user cancels |

### 3.2 Protocol rules
- Every task carries `workflow_id`, `task_id`, `parent_task_id`, `tenant_id`, `assigned_agent`, `status`, `deadline`, `idempotency_key`, `rework_round` and `trace_id`.
- Agents receive the minimum required context and evidence IDs, not entire documents by default.
- Validate every output against a versioned JSON schema before accepting it.
- Agent result statuses: `completed`, `needs_more_information`, `blocked`, `retryable_error`, `final_error` (mapping to task states is defined in `MVP_PLAN.md`).
- Only the Supervisor changes task and workflow state. The exceptions are mechanical: a worker claims a task (`QUEUED → RUNNING`) and lease expiry returns it to `QUEUED`. Agents submit results and proposed next actions.
- Numbers in messages are decimals serialised as strings (`"12.5"`); unit, scale, currency and period are separate fields.
- Bounded retries with exponential backoff for transient errors; do not retry schema or permission errors indefinitely.
- Persist model/prompt versions, tool calls, inputs/outputs, token usage, latency and trace IDs.

### 3.3 Example task message
```json
{
  "schema_version": "1.0",
  "workflow_id": "wf_123",
  "task_id": "task_456",
  "parent_task_id": null,
  "rework_round": 0,
  "idempotency_key": "wf_123:task_456:0",
  "tenant_id": "tenant_001",
  "assigned_agent": "financial_analyst",
  "objective": "Compare revenue and operating margin for FY2023-FY2025",
  "inputs": {
    "document_version_ids": ["docv_01", "docv_02"],
    "evidence_ids": ["ev_101", "ev_102"],
    "periods": ["FY2023", "FY2024", "FY2025"]
  },
  "dependencies": ["task_extract_001"],
  "acceptance_criteria": [
    "All values have source evidence",
    "Calculations include formulas and units"
  ],
  "constraints": {
    "max_attempts": 3,
    "timeout_seconds": 180,
    "requires_human_approval": false
  },
  "deadline": "2026-11-20T10:30:00Z",
  "trace_id": "trace_abc"
}
```

### 3.4 Example agent result
```json
{
  "schema_version": "1.0",
  "workflow_id": "wf_123",
  "task_id": "task_456",
  "agent_name": "financial_analyst",
  "status": "completed",
  "findings": [{
    "finding_id": "finding_001",
    "claim": "Revenue increased by 12.5% from FY2024 to FY2025",
    "value": "12.5",
    "unit": "percent",
    "calculation": {
      "calculation_id": "calc_001",
      "function": "pct_change",
      "formula": "(current - previous) / previous * 100",
      "inputs": {
        "current":  {"value": "900", "unit": "currency", "currency": "INR", "scale": "crore", "period": "FY2025", "evidence_id": "ev_101"},
        "previous": {"value": "800", "unit": "currency", "currency": "INR", "scale": "crore", "period": "FY2024", "evidence_id": "ev_102"}
      },
      "output": "12.5",
      "calc_version": "1"
    },
    "evidence_ids": ["ev_101", "ev_102"],
    "verification_status": "pending"
  }],
  "warnings": [],
  "errors": [],
  "next_action": "submit_for_verification",
  "metrics": {"latency_ms": 4200, "input_tokens": 2300},
  "trace_id": "trace_abc"
}
```
Example numbers are illustrative. An agent-reported confidence value is not a calibrated probability unless separately evaluated.

## 4. Database schema (PostgreSQL)

PostgreSQL holds transactional workflow and metadata. Qdrant is a separate vector index; object storage holds originals and derived artifacts. Tenant isolation and access controls from the beginning.

| Table | Key columns | Purpose |
|---|---|---|
| tenants | id, name, created_at | Organization boundary |
| users | id, tenant_id, email, role | Identity and access |
| companies | id, tenant_id, name, industry, jurisdiction | Enterprise entity |
| documents | id, tenant_id, company_id, filename, document_type, status | Logical document |
| document_versions | id, tenant_id, document_id, version_number, checksum (SHA-256, unique per document), filename, size_bytes, storage_uri, uploaded_at, is_current (at most one per document, unique partial index), parser_status | Immutable versions and supersession; each version is one stored file |
| document_chunks | id, tenant_id, version_id, chunk_index, page_start, page_end, section, text, metadata, qdrant_point_id | Chunk metadata and vector link |
| evidence_records | id, tenant_id, version_id, chunk_id, page, section, bbox, quote, source_uri | Traceable source evidence; `bbox` is the polygon in PDF points from the parser, null when unknown |
| workflows | id, tenant_id, user_id, company_id, query, status, plan_json, trace_id, timestamps | User analysis request |
| agent_tasks | id, tenant_id, workflow_id, parent_task_id, agent_name, objective, status, input_json, output_json, attempts, max_attempts, rework_round, idempotency_key (unique), lease_expires_at, deadline, trace_id | Agent work units |
| task_dependencies | task_id, depends_on_task_id | Task graph edges |
| task_transitions | id, task_id, from_status, to_status, reason, actor, created_at | Every persisted state change (workflow changes go to `audit_events`) |
| agent_runs | id, task_id, attempt, model, prompt_version, trace_id, tool_calls_json, token_usage, latency, error | Execution audit |
| findings | id, workflow_id, task_id, category, claim, value_json, unit, period, verification_status | Structured claims |
| finding_evidence | finding_id, evidence_record_id, relation_type | Claim-to-evidence mapping |
| calculations | id, finding_id, task_id, function, formula, inputs_json, output, unit, calc_version | Deterministic calculation trace; Verification replays it |
| verification_results | id, finding_id, task_id, decision, reasons_json, corrected_refs_json, rework_instructions, verified_at | Claim-level verification outcome |
| risk_assessments | id, workflow_id, finding_id, category, likelihood, impact, severity, methodology_version | Risk register |
| regulatory_requirements | id, jurisdiction, framework, version, requirement_code, effective dates | Versioned requirements |
| compliance_mappings | id, workflow_id, requirement_id, status, rationale, reviewed_by | Requirement mapping |
| reports | id, workflow_id, version, storage_uri, status, generated_at | Report artifacts |
| human_reviews | id, workflow_id, task_id, reviewer_id, decision, comments, reviewed_at | Approval trail |
| audit_events | id, tenant_id, actor, action, resource, timestamp, details_json | Audit log |

### 4.1 Qdrant payload
```json
{
  "tenant_id": "tenant_001",
  "company_id": "company_123",
  "document_id": "doc_456",
  "document_version_id": "docv_002",
  "document_type": "annual_report",
  "financial_year": 2025,
  "page_start": 45,
  "page_end": 46,
  "section": "Consolidated income statement",
  "access_scope": ["finance", "compliance"],
  "is_current": true
}
```
- Apply tenant and access-scope filters to every vector query.
- On replacement: retain old version for audit → index new version → verify indexing → switch current-version pointer.
- Default retrieval excludes superseded versions.

## 5. API endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/documents` | Upload document and create initial version |
| `POST /api/v1/documents/{id}/versions` | Upload replacement and start re-indexing |
| `GET /api/v1/documents/{id}/versions` | List versions and processing status |
| `POST /api/v1/workflows` | Create analysis workflow |
| `GET /api/v1/workflows/{id}` | Read workflow status and plan |
| `GET /api/v1/workflows/{id}/tasks` | Inspect task graph and agent progress |
| `GET /api/v1/workflows/{id}/findings` | Read findings and evidence references |
| `POST /api/v1/workflows/{id}/reviews` | Submit human approval/rejection |
| `GET /api/v1/workflows/{id}/report` | Retrieve report metadata or download location |

## 6. Security, reliability and governance
- **Multi-tenancy:** tenant and authorization checks in every API, SQL query, vector search, object-storage operation and background job.
- **Document safety:** uploaded and retrieved text is untrusted data. Ignore embedded instructions that try to control agents or reveal secrets.
- **Tool safety:** allowlist tools and destinations, validate arguments, set timeouts/rate limits, sandbox code execution.
- **Secrets:** managed secret store or environment injection; never in prompts, source control, logs or reports.
- **Auditability:** record uploads, version changes, workflow starts, approvals and report exports.
- **Human review:** required for material compliance conclusions, high-impact risk classifications and externally distributed reports.
- **Failure handling:** distinguish transient errors, invalid data, permission failures, missing evidence and model refusal.
- **Prompt-injection defense:** separate system instructions from retrieved content; retrieved text can never invoke tools directly.
- **Retention:** define retention/deletion rules for originals, extracted text, embeddings, logs and reports.

## 7. Evaluation and acceptance criteria

| Area | Measure | Evaluation approach |
|---|---|---|
| Extraction | Field and table accuracy; page provenance | Gold set of native and scanned PDFs |
| Retrieval | Recall@K, MRR, evidence relevance | Questions with known supporting pages |
| Financial | Calculation correctness; period/unit consistency | Replay formulas against independent expected results |
| Regulatory | Mapping precision; unsupported-gap rate | Expert-labelled, versioned requirements |
| Risk | Precision/recall; rationale quality | Labelled scenarios and false-positive review |
| Verification | Unsupported-claim detection; citation correctness | Inject wrong claims and page references |
| Orchestration | Completion, recovery, dependency correctness | Test timeout, missing input, partial failure |
| Efficiency | Latency, token cost, cost per workflow | Compare with single-agent RAG baseline |

## 8. Full implementation sequence (target design)
1. **Data foundation:** FastAPI, authentication, upload, versioning, object storage, OCR/layout/table extraction, PostgreSQL, Qdrant, hybrid retrieval.
2. **Supervisor and contracts:** agent registry, task/result schemas, workflow state machine, dependency graph, trace IDs, timeout and retry handling.
3. **First specialist agents:** Document Intelligence, Financial Analyst, Research. Prove end-to-end flow before expanding.
4. **Regulatory and risk intelligence:** Regulatory Intelligence and Risk Analysis with versioned knowledge and documented scoring.
5. **Verification and reporting:** claim-level verification, rework loop, report generation, citation resolver, human review.
6. **Production hardening:** tenant isolation, security tests, observability, benchmark suite, cost controls, Docker, CI/CD, Kubernetes.

> `MVP_PLAN.md` reorders this: Verification and the baseline benchmark come before Research, Regulatory and Risk.

## 9. Definition of done — first complete release
- [ ] A user uploads multiple company reports and sees version-processing status.
- [ ] A complex financial + ESG question produces a visible task plan.
- [ ] The Supervisor selects agents dynamically and respects dependencies.
- [ ] Specialist agents return schema-valid outputs with evidence IDs.
- [ ] Financial calculations are deterministic and reproducible.
- [ ] Verification rejects unsupported claims and triggers bounded rework.
- [ ] The final report includes citations, limitations and review status.
- [ ] Workflows resume after worker restart without duplicating completed work.
- [ ] Evaluation is repeatable and compared with a single-agent RAG baseline.

## Final recommendation
Build incrementally. Start with the data foundation and Supervisor, then add specialist agents one at a time. Keep tools and data services deterministic, require evidence for claims, and use benchmark results, not agent count, to demonstrate quality.

*This is a proposed design, not evidence of implementation. Validate framework versions, security requirements, data licensing, regulatory applicability and deployment constraints before production use.*
