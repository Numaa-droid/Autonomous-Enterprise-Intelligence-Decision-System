# AEIDS — Decision Log

Record each design or library choice in a few lines: what was chosen, what else was considered, and why. Newest at the top.

## Template
```
### D-XXX — <title>
- Date: YYYY-MM-DD
- Status: proposed | accepted | superseded by D-YYY
- Decision:
- Alternatives considered:
- Why:
- Consequences / follow-ups:
```

---

## Open decisions

### D-003 — Orchestration: hand-written vs framework (e.g. LangGraph)
- Status: decided in D-024 (hand-written)
- Leaning: hand-written state machine persisted in Postgres, so transitions, retries and resume logic are fully owned and explainable

### D-006 — PDF parsing / table extraction
- Status: proposed
- Decision: reuse the existing TableLens pipeline in this repo (Table Transformer detection + docling-layout-heron fusion; fine-tuned Table Transformer structure model; PaddleOCR-VL-1.6 for scans and charts) behind an `aeids/ingestion/` interface.
- Alternatives considered: docling's full document converter, pdfplumber/Camelot (native PDFs only), a hosted service such as Azure Document Intelligence (removed from the repo in `a4531cf`).
- Why: already built and measured (fine-tuned model on held-out reports: rows 82.7 %, columns 82.7 %, cell adjacency F1 94.0 %); gives page numbers and boxes for evidence; handles scanned and sideways tables.
- Consequences / follow-ups: heavy dependencies (torch, paddle) pinned for Python 3.11; recognition needs a GPU (local or Modal `gpu_server.py`), so ingestion is slow on CPU; AEIDS reads the generic `_structure.json`, not the medical reference JSON (D-010); measure table accuracy on the benchmark corpus's financial statements.

### D-009 — Tracing (see D-022: own spans in Postgres for now)
- Status: open
- Options: Langfuse, OpenTelemetry + a viewer

---

## Proposed decisions

### D-011 — Separate task and workflow state machines; rework as a child task
- Date: 2026-11-20
- Status: accepted (implemented in M3, D-024)
- Decision: task and workflow states are two state machines with explicit transition tables (`ARCHITECTURE.md` §3.1). A rejected task is never re-run; rework is a new task with `parent_task_id` and `rework_round`. Workers hold a lease; an expired lease returns the task to `QUEUED`.
- Alternatives considered: one combined lifecycle (the first draft); re-running the rejected task in place.
- Why: the first draft had no exit from `BLOCKED` / `NEEDS_HUMAN_REVIEW`, no path for tasks without findings, and mixed workflow states (`REPORTING`, `COMPLETED`) into tasks. A new rework task keeps the rejected attempt auditable.
- Consequences / follow-ups: tables `task_dependencies`, `task_transitions`; columns `idempotency_key`, `rework_round`, `lease_expires_at` on `agent_tasks`.

### D-012 — Numbers as Decimal strings with separate unit, scale and currency
- Date: 2026-11-20
- Status: proposed
- Decision: all numeric values are `Decimal`, serialised as JSON strings. Calculation inputs and outputs are `Quantity` objects (value, unit, scale, currency, period, evidence_id), stored in a `calculations` table.
- Alternatives considered: JSON numbers (float precision loss); fused unit strings such as `INR_crore`.
- Why: Verification must replay calculations and check unit, scale and period of every input, which a bare `dict[str, Decimal]` cannot carry.

## Accepted decisions

### D-039 — Read-only demo mode
- Date: 2026-11-27
- Status: accepted
- Decision: `AEIDS_READ_ONLY=true` adds `ReadOnlyGuard` (`aeids/api/security.py`): every POST, PUT, PATCH or DELETE is refused with 403 `read_only` unless it is on an allow-list of four routes: `POST /api/v1/search`, `POST /api/v1/workflows` and their console twins under `/api/v1/dashboard`. `GET /api/v1/dashboard/tenants` reports `read_only`, and the console then shows a note instead of the upload panel and hides Delete, Upload a newer version, Try again and the review buttons.
- Alternatives considered: login for editors (more than a demo needs, out of Release 0 scope); a deny-list of the current write routes (a route added later would be writable by default).
- Why: the public Render demo linked from a resume had no login, so any visitor could upload or delete reports.
- Consequences / follow-ups: questions still call OpenRouter, so a visitor spends the key's credit; keep a spending limit on the key (a per-visitor rate limit is not built). The owner uploads by switching the flag off, or with the API from a trusted machine while it is off.

### D-038 — Deleting a report archives it
- Date: 2026-11-27
- Status: accepted
- Decision: `DELETE /api/v1/documents/{id}` (and a Delete button with a confirmation on the console's report card) sets `documents.status` to `archived` (`aeids.documents.archive_document`). Archived reports are left out of `GET /api/v1/documents`, the console list, `get_document` (404), new versions, upload dedupe, the agents' company list and ESG sources (`AgentContext.companies`, `esg_sources`); search already required `status = active`. Their Qdrant points are deleted best effort (if Qdrant is down the delete still succeeds; search re-checks Postgres).
- Alternatives considered: erasing the rows, chunks and stored file (breaks the evidence of findings and reports that cite the report, rule 1); only flipping `is_current` off (a state change outside the versioning rule, and the report would still be listed).
- Why: a report uploaded before the Render deployment could process it stayed "Waiting" with no way to remove it.
- Consequences / follow-ups: there is no undelete; an archived report's rows and stored PDF stay until a retention job exists. Re-uploading the same PDF creates a new report (dedupe skips archived ones).

### D-037 — Render demo deployment: one service, inline jobs, Qdrant Cloud, TableLens over a tunnel
- Date: 2026-11-27
- Status: accepted (demo hosting, not production)
- Decision: on Render, aeids-api runs migrations in its start command (`alembic upgrade head && uvicorn …`), uses `AEIDS_JOBS_MODE=inline` (the API parses and indexes an upload in the request; no worker, no Redis), Qdrant Cloud (`AEIDS_QDRANT_URL` + new `AEIDS_QDRANT_API_KEY`, read by `VectorIndex.from_settings`), and reaches TableLens on the developer's Mac through a Cloudflare tunnel (`AEIDS_TABLELENS_URL`). `.python-version` pins Python 3.11 (Render defaulted to 3.14).
- Alternatives considered: a Render Background Worker + Key Value (no free worker plan, and it cannot read the API's local `storage/` disk); TableLens on Modal (always on, more setup; still the path for a lasting deployment).
- Why: the first deploy failed with `relation "tenants" does not exist` (no migrations), then uploads waited forever (no worker, Redis, Qdrant or TableLens reachable).
- Consequences / follow-ups: parsing works only while the Mac, TableLens and the tunnel run; a quick-tunnel URL changes on each restart (update `AEIDS_TABLELENS_URL`). Render's disk is ephemeral, so uploaded PDFs are lost on redeploy (parsed chunks stay in Postgres and Qdrant). The tunnel URL is unauthenticated: anyone who has it can send PDFs to TableLens.

### D-036 — Countries and regulators are not companies; a company without a report is refused for quote answers too
- Date: 2026-11-24
- Status: accepted
- Decision: Document Intelligence drops planner company entries that are a country, region, government or regulator (`NOT_COMPANIES`, `is_company` in `aeids/agents/document_intelligence/agent.py`: US, China, EU, SEC, SEBI, …) before the "no report has been uploaded for X" check. A real company without an uploaded report is still refused, for number and passage (quote) answers alike; tests cover both.
- Why: with only Tesla uploaded, the live planner listed "US" in "what are the risks from US export controls?" as a company with no report and the question was refused. The NVIDIA risk question that was answered from Tesla's report (2026-11-20) ran on the M3 stand-in agent (`0.1-stub`), which has no model and cannot recognise company names; the LLM agent refuses it.
- Consequences / follow-ups: the stand-in agents (used only when no OpenRouter key is set, `AEIDS_AGENT_MODE=auto`) still cannot refuse companies they do not know. The list is code, not the prompt, so the benchmark prompts are unchanged.

### D-035 — Document type is checked on upload; ESG questions need an ESG source
- Date: 2026-11-24
- Status: accepted
- Decision:
  - **Upload** (`aeids/doctype.py`, `documents.check_document_type`, both upload routes):
    - AEIDS reads the text of the first 5 pages and up to 10 sampled pages (pypdf, under 1 s on the corpus).
    - It classifies the PDF by the title that comes first: "Form 10-K" / "20-F" / "Annual Report", "Sustainability / ESG / Impact Report", or "Business Responsibility and Sustainability Report".
    - Without a title, the content decides: financial statements and auditor's reports; Scope 1/2, GHG, GRI, TCFD; or BRSR's general disclosures, principles and indicators.
    - A cross-family choice (annual vs ESG / BRSR) is refused with 422 `document_type_mismatch` and the reason ("page 1 says "Form 10-K""). The staged file is discarded.
    - A new version must be of the document's type.
    - Not checked: a PDF without text (scanned), and type `other`.
    - An annual report that contains its BRSR (common in India) is an annual report: uploading it as a BRSR is refused with a hint to upload it as an annual report.
  - **Questions:**
    - `needs_esg_report` decides which questions are ESG-only: emissions, energy use or intensity, renewable energy, water, waste, LTIFR / injuries, CSR spend, BRSR, sustainability. Headcount, board members and "energy storage" are not: annual reports disclose them.
    - Document Intelligence answers such a question only from companies with an ESG source (`AgentContext.esg_sources`): a current ESG report or BRSR, or an annual report with a BRSR / sustainability section heading.
    - If no company in scope has one, it stops before any model call. If a named company has none, it stops too. Either way the task waits for a person with the reason and what to upload, the same as for a company with no report. Re-run works after the upload.
    - If no company is named, only companies with an ESG source are searched.
- Alternatives considered:
  - Asking the model to classify the PDF: slower, costs tokens, and is not deterministic.
  - Checking only after parsing: the wrong type would already be stored and processed.
  - Answering ESG questions from annual reports: 10-Ks mention climate only in passing, so answers would be guesses.
- Why: the user asked that an annual report uploaded as an ESG report be refused, and that an ESG question about annual reports not be answered.
- Consequences / follow-ups:
  - Tests in `tests/test_doctype_d035.py`; the 7 that expect a refusal fail without the checks.
  - All five corpus 10-Ks are classified as annual reports.
  - None of the 68 benchmark questions is ESG-only.

### D-034 — Security review: local-only request guard, hidden database URL, private .env, strong database password
- Date: 2026-11-24
- Status: accepted
- Decision:
  - **Request guard:** `aeids/api/security.py` (`LocalOnlyGuard`, added in `create_app`):
    - **Host check:** refuses a request whose `Host` is not in `AEIDS_ALLOWED_HOSTS` (default `127.0.0.1`, `localhost`, `::1`) with 400 `host_not_allowed`. This closes DNS rebinding, where a site points its own name at 127.0.0.1 and reads the API as that site.
    - **Cross-site writes:** refuses a POST, PUT, PATCH or DELETE that a browser marks as coming from another site (`Origin` not an allowed host, `Origin: null`, or `Sec-Fetch-Site: cross-site`) with 403 `cross_site_request`. A page on another site could otherwise upload a PDF with a plain form POST and spend embeddings.
    - **Not affected:** requests without those headers (curl, scripts, the CLI) and the console itself (same origin).
  - **Hidden database URL:** `Settings.database_url` is `repr=False`, so printed settings and tracebacks never show the database password. The OpenRouter key was already a `SecretStr`.
  - **Local fixes:**
    - `.env` set to mode 600;
    - the Postgres role's placeholder password replaced with a random 32-character one (`ALTER ROLE`, and the same in `.env`);
    - `.env.example` now says to use a strong password and `chmod 600`.
- Alternatives considered:
  - Starlette's `TrustedHostMiddleware`: it mishandles `[::1]:port`.
  - CSRF tokens: heavier, and the console has no login or sessions.
  - CORS middleware: AEIDS needs no cross-origin access, and without CORS headers browsers already withhold responses.
- Why: a security review found the OpenRouter key well protected:
  - never committed, logged, traced or printed;
  - sent only to OpenRouter, and scrubbed from errors;
  - a USD 3 lifetime spending cap on the key, no request-rate limit (paid key), expiry 2027-02-18.
  But the local API could be reached from a web page in the user's own browser.
- Consequences / follow-ups:
  - Tests: `tests/test_security_d034.py` (foreign hosts, cross-site uploads and questions, same-origin and scripted writes, printed settings). The guard tests were confirmed to fail without the guard.
  - To serve the API under another name (a reverse proxy), add that name to `AEIDS_ALLOWED_HOSTS`.
  - Real authentication stays deferred (Release 0 scope).

### D-033 — Earnings per share in the knowledge base (1.1); per-share labels match only per-share line items; an ambiguous-label benchmark question
- Date: 2026-11-23
- Status: accepted
- Decision:
  - **New line items:** `basic_eps` and `diluted_eps`, under the names the corpus prints ("Diluted net income per share", "Net income per diluted share", "Earnings per share – diluted", Ind AS "… per equity share", …). The bare row labels "Basic" / "Diluted" are not aliases: the share-count rows use them too, so the model picks those cells, seeing the heading. Knowledge base version 1.0 → 1.1.
  - **Label matching:** in `line_item_for`, a label that names a per-share amount ("per share", "per diluted share", "EPS") matches only a per-share line item, and the reverse. A label containing the word "shares" is a share count, never EPS. A label the code cannot place safely returns no match instead of a wrong one. Before this, "Net income per share: Diluted" resolved to Net income.
  - **New benchmark question:** c11 asks for Tesla's FY2025 operating margin, whose income line is printed as "Income from operations" (the D-032 defect).
- Alternatives considered: the bare "Basic" / "Diluted" aliases (they would match share counts); leaving EPS out (the named-line-item safeguard cannot catch a line item the knowledge base lacks).
- Why: Release 0 review open item 2. l03 (Apple's diluted EPS) was right in only 2 of 3 runs, and no question covered an ambiguous label.
- Consequences / follow-ups:
  - Tests in `tests/test_knowledge.py`: per-share labels from all five reports, share-count rows, the named-line-item safeguard.
  - Benchmark with 37 questions: l03 and c11 passed in 4 of 4 runs.
  - The same run showed one miss of another kind: c08 (Tesla gross margin), where the analyst model bound revenue to the gross-profit input. The label check refused it, and the report listed "Could not be calculated …" (D-032). Possible follow-up: re-wire a metric's inputs by their line items when the model swaps them.

### D-032 — Release 0 review fixes: exact template summaries, labels settled by extraction, no silent calculation gaps
- Date: 2026-11-23
- Status: accepted
- Decision:
  - **Template summaries:** a summary written by code must be exactly what `template_summary` writes from the verified findings. Only an AI summary goes through the number check. That check had refused the passage count in "… in 5 passages …" and failed every answer made only of quotes.
  - **Reports for older workflows:** if a report requested for an older workflow fails its check, the API returns 422 `report_check_failed` instead of a 500.
  - **Ambiguous labels in the Financial Analyst:** a label printed for more than one line item ("Income from operations") counts for a line item only when it is one of that item's names *and* Document Intelligence extracted the fact for that item.
  - **Refused calculations:** a calculation the analyst was asked for but refused is returned as `not_calculated`. The Supervisor lists it as "Could not be calculated from the verified figures: …", which makes the report partial.
- Alternatives considered:
  - Dropping the number check for code-written text: weaker than exact equality.
  - Removing "Income from operations" from revenue's aliases: Ind AS reports print it for revenue.
  - Leaving the analyst's warning in task output only: the reader never sees it.
- Why: found by the Release 0 review ([RELEASE_0_REVIEW.md](RELEASE_0_REVIEW.md)). Tesla's operating margin was silently missing from a report marked "Complete".
- Consequences / follow-ups: regression tests in `tests/test_report_m7.py`; each was confirmed to fail before its fix. Open items from the review: add earnings per share to the knowledge base, and add ESG / BRSR documents and an ambiguous-label question to the benchmark.

### D-031 — M7 reports: built from verified findings, checked by code, stored as JSON + Markdown in Postgres
- Date: 2026-11-23
- Status: accepted
- Decision:
  - **Built:** `aeids/reporting/report.py` builds a `ReportDoc` (schema `report/1.0`) from the workflow's verified findings:
    - status: complete, partial or nothing verified;
    - the summary, marked as AI-written or a template;
    - calculated results, reported figures and cited passages;
    - each calculation with its typed inputs;
    - numbered citations (evidence id, file, company, version and whether it is still current, page, section, quote, bbox);
    - limitations;
    - review: automatic counts, rework rounds, and each human decision from the audit log;
    - generation metadata: models, prompt and agent versions, calc versions, tokens, cost, trace id.
  - **Checked by code against the database before storing:**
    - every line is a verified finding of the workflow, and every verified finding is in the report;
    - every line cites a source;
    - every source is an evidence record of the tenant, with the stated version and page, that supports a finding in the report;
    - every calculation equals the stored one;
    - every number in the summary is a checked value.
  - **What a failure does:** the Supervisor does not store the report and moves the workflow REPORTING → FAILED, with the reasons in `error`. That transition was already legal.
  - **Rendered:** Markdown (`md-v1`). Text taken from documents is collapsed to one line and has `\ ` * _ [ ] < > |` escaped, so it cannot become a link, image, HTML, emphasis or a table cell (rule 7).
  - **Stored:** table `reports` (migration 0007): `version` per workflow, content JSON, Markdown, sha256.
  - **Served:**
    - `GET /api/v1/workflows/{id}/report` returns metadata and content; 409 until the workflow completes.
    - `GET .../report.md` returns the file with a sha256 ETag.
    - The console route `/api/v1/dashboard/workflows/{id}/report.md?tenant=` serves the eval workspace, behind the answer page's download button.
    - CLI: `python -m aeids.reporting <id> [--tenant] [-o] [--json]`.
  - **Older workflows:** a workflow completed before M7 gets its report on first request.
- Alternatives considered:
  - An LLM-written report: that would add wording and numbers the verifier never saw. The only model text is the D-028 summary, and its numbers are checked again here.
  - Files in the object store with a `storage_uri`, as ARCHITECTURE §4 sketches: reports are a few KB of text, and keeping them in Postgres keeps them in the same transaction as the workflow's completion.
  - PDF output: out of scope for Release 0.
- Why: the Release 0 definition of done says the report contains only verified findings, with citations and limitations. Before M7 this was true by construction but never checked, and there was no report file.
- Consequences / follow-ups:
  - Tests in `tests/test_report_m7.py`: complete, partial (a dropped finding), human-reviewed and nothing-verified reports; each check refusing a tampered report; a failed check failing the workflow; versioning and on-request backfill; hostile document text staying inert.
  - Re-rendering, for example after a cited version is replaced, would add version 2; there is no endpoint for it yet.

### D-030 — Fewer and smaller model calls: reasoning off, code picks clear cells, skipped calls
- Date: 2026-11-23
- Status: accepted
- Decision:
  - **Reasoning off by default.** Chat requests send `reasoning: {"enabled": false}` (`AEIDS_LLM_REASONING`: off / low / medium / high / model_default). Call timeout 120 s → 45 s (`AEIDS_LLM_TIMEOUT_SECONDS`), still retried within `llm_max_retries`.
  - **Code picks the table cell when the match is clear** (`aeids/agents/document_intelligence/resolver.py`, `kb-match-v1`). A cell is picked when its row label (footnote marks stripped) is exactly one of the line item's printed names from the knowledge base, that name belongs to no other line item, the cell has a fiscal-year period and a fitting unit, and the candidates in primary statements (else all tables) hold one value. A line item is resolved for every company and year or not at all; the rest goes to the model with the unchanged extract prompt. Rework rounds always use the model. Facts record `picked_by` (code / model); the verifier checks both the same way. `AEIDS_DI_CELL_RESOLVER=model` restores the old behaviour.
  - **Smaller planning prompt.** `knowledge.planning_catalog(kb, question)` lists only financial-statement line items and the metrics built from them unless the question mentions an ESG / BRSR topic (11.6k → 6.4k characters).
  - **Skipped calls.** The Financial Analyst returns "no calculation asked for" without a model call when the question asks for none (`needs_calculation`; never on a rework round). The report writer uses its template for an answer of at most two reported figures and no calculations or passages.
  - **Batched query embeddings.** `embed_queries` sends one request for the queries an agent will search, with an in-process memo in front of the SQL cache.
  - **Fixes found by the benchmark.** Document Intelligence looks at 15 search hits and keeps up to 6 tables per query (the income statement of one report ranked 8th). When the planner lists no line item but the question names one word for word (`CellResolver.named_in`), it is searched anyway.
- Alternatives considered: a smaller or different model (accuracy risk, and the cost was mostly in unneeded calls); caching whole answers (does not help new questions); letting code pick every cell (ambiguous names such as "Income from operations" mean revenue in some Ind AS reports and operating income in US GAAP ones).
- Why: profiling showed Document Intelligence's cell picking was 70% of the time and 83% of the tokens, although TableLens had already read the tables; hidden reasoning made each call 2–3× slower with the same answers.
- Consequences / follow-ups: benchmark (D-029) before → after: accuracy 89% → 92%, median 22.1 s → 4.6 s, p95 59 s → 10.5 s, tokens per question 9,544 → 4,553, cost USD 0.0017 → 0.0004; unsupported numbers stay 0%. Tests in `tests/test_speed_d030.py`. Outliers left are provider variance (a retried chat call, a slow embedding).

### D-029 — M6: single-agent RAG baseline and an end-to-end benchmark
- Date: 2026-11-23
- Status: accepted
- Decision:
  - **Baseline** (`aeids/baseline/`, prompt `rag-v1`): the same hybrid search (top 8), the same model and one prompt that answers in JSON with cited source ids. Passages are wrapped as untrusted data; one retry on a schema error; unknown cited ids are dropped. CLI: `python -m aeids.baseline "question" --tenant eval`.
  - **Questions** (`eval/questions.jsonl`, 36): 14 lookups, 10 calculations, 6 comparisons across companies, 6 not in the reports. Each has expected values (value, unit, scale, currency) and gold evidence as regex patterns over page text; `run_benchmark.py check-gold` re-derives the gold pages from the TableLens output.
  - **Scoring** (`aeids/evaluation/scoring.py`), the same for both systems: an answer is correct when every expected value is stated (scale-aware; tolerance 0.5% for amounts and counts, 0.5 pp for percentages, 0.01 for ratios, 0.005 per share) and, for not-found questions, when it says so. Citation precision is against gold pages; a stated number is unsupported when it is printed on no cited page (AEIDS calculation outputs that verification replayed count as supported). Latency, tokens and cost are summed from each run's trace. A workflow stopped for human review counts as no answer, not an error.
  - **One command**: `.venv/bin/python eval/run_benchmark.py` runs both systems on the `eval` tenant and writes `reports/benchmark-<time>.json` and `.md`; `rescore <file>` re-scores a saved run.
- Alternatives considered: an LLM judge (non-deterministic, and it would grade a model with a model); exact string matching of answers (fails on "$416.2 billion" vs "416,161 million").
- Why: Release 0's question is whether the multi-agent pipeline beats one RAG prompt, measured the same way on both.
- Consequences / follow-ups: result (after D-030): AEIDS 92% vs baseline 69% (lookups 79/79, calculations 100/80, comparisons 100/0, not found 100/100); answers with a correct citation 93% vs 63%; unsupported numbers 0% vs 12%; median 4.6 s vs 2.0 s; 4,553 vs 3,395 tokens; USD 0.0004 vs 0.0002 per question. AEIDS misses the three headcount questions, whose values are printed only in prose (not extracted yet).

### D-028 — The answer page: a plain-English summary guarded to checked numbers
- Date: 2026-11-22
- Status: accepted
- Decision: the question page leads with the answer for non-specialists: question, one plain status line, progress steps while running, an **Answer** card (calculated results, figures used with year and page, quotes), a review box only when a person is needed; task graph, findings and timeline fold into "How AEIDS got this answer". `aeids/reporting/answer.py` arranges verified findings into that answer (API `answer` field). A last workflow step, `summarise` (`report_writer`, prompt `rw-v1`), writes 2-4 plain sentences from verified findings only; code checks that every number in them is a checked value as displayed (or a year), retries once naming the offending numbers, else uses a template that restates the checked values. The summary is labelled as written by AI; it adds no fact, so it is not a finding. Without a writer registered (custom registries in tests) the plan has no summarise step. The OpenRouter client now asks again on an empty reply (a reasoning model that returned no text), bounded by `llm_max_retries`.
- Why: non-technical readers need the answer in words; the number guard keeps rule 2 (no model-made numbers) for prose too.
- Consequences / follow-ups: the guard checks numbers, not wording; the prompt forbids causes, forecasts and advice. Live: Apple ROE, Apple revenue and margin, Microsoft FCF and D/E, Tesla risks all summarised by AI with every number checked.

### D-027 — M5 verification: deterministic checks against the parsed tables, bounded rework, human review
- Date: 2026-11-22
- Status: accepted
- Decision:
  - **Verification agent** (`aeids/agents/verification/`, deterministic, no model): reads findings, their cited evidence, the chunks and the stored calculations from the database (not from the task payload). Evidence: exists for the tenant, quote in its chunk, page inside the chunk, version still current, document active; values read off a chart image need a person. Reported values: the quoted row is found in the chunk's normalised table and the evidence box picks the cell; value, unit, currency, scale and period must equal the cell's, the evidence page must be the cell's page and the claim must show the printed value. Calculations: replayed with `aeids.calc` from the stored inputs (output, and the finding's value, unit, scale, period must agree); every input with an evidence id must be cited and equal its cell; an input without one must be an earlier step's output of the same task (or an `intensity` constant); the claim must show the computed value. Decision: any failed check `rejected` (reasons + rework instructions), unverifiable `needs_review`, else `verified`. Document Intelligence and Verification read cells through one shared module (`aeids/agents/cells.py`).
  - **Rework** at most 2 rounds (`AEIDS_WORKFLOW_MAX_REWORK_ROUNDS`, default 2). With no rounds left, a task whose other findings verified becomes VERIFIED and its rejected findings are dropped (listed in the report as "Dropped after verification"); dependents get only verified facts (the runner filters them). A task with nothing verified stays REJECTED and blocks its dependents.
  - **Human review**: `POST /api/v1/workflows/{id}/reviews` `{task_id, decision: approve|reject|rerun, note, reviewer}` for a task in NEEDS_HUMAN_REVIEW. Approve verifies its open findings, reject rejects them and fails the task, rerun re-queues a task without findings (e.g. after the missing report was uploaded; a task with findings cannot be re-run, it would duplicate them). Each decision is a `verification_results` row per finding, a task transition with actor `reviewer:<name>` and an audit event. The console's Workflows page has the review panel and shows each finding's calculation.
  - **Analyst guards found by the live run**: a knowledge-base metric binds a fact only if its row label is a known name of the line item the metric expects; a basic calculation whose title names a metric with defined line items is refused (it must go through the recipe). An empty model reply (`LLMResponseError`) is transient: the task is retried with backoff.
- Alternatives considered: an LLM judge for verification (non-deterministic, and it would check a model with a model); retrying a rejected task in place (loses the record of what was wrong; rework is a child task, D-011); failing the whole task when one finding stays wrong (throws away verified work).
- Why: rule 1 and rule 2 are only as strong as their check; every fact is now checked against the cell it came from and every number is recomputed.
- Consequences / follow-ups: done-when met in `tests/test_verification_m5.py`: wrong number, wrong page, superseded version, unit, scale, currency and period mismatches, wrong citation, a claim that shows another number, a tampered calculation output, a consistent lie about an input, an uncited input and a quote not in its source are all rejected, and rework recovers; a fault that persists is dropped after two rounds. Live: Apple's and Microsoft's real 10-K extractions verified untouched. Verification checks values, citations and arithmetic, not whether a label is the right concept; that is enforced when a knowledge-base metric is bound (above). A metric id on findings (to re-check that in verification) would need a contract change.

### D-026 — Metric knowledge base (`aeids/knowledge/metrics.yaml`): definitions in YAML, numbers in code
- Date: 2026-11-22
- Status: accepted
- Decision:
  - **One reviewed file** holds what the agents need to know about metrics: 51 **line items** (US GAAP, Ind AS and BRSR names, statement, expected unit, sign convention for outflows, and the BRSR ratios companies print themselves, linked to the metric that recomputes them) and 41 **metrics**: profitability, growth, returns, leverage, liquidity, cash flow, efficiency, BRSR environment (energy, water, waste and Scope 1+2 emission intensity per rupee of turnover, renewable energy share, waste recovery), BRSR people (women employees and directors, well-being spend, LTIFR) and CSR (spend vs average net profit, vs obligation). Each metric has a formula text, its inputs (line items), period rules (`same` year, or `later`/`earlier` for opening balances and growth) and a **recipe**: steps that each call one `aeids.calc` function, with intermediate steps named (e.g. `avg_equity`).
  - **The file cannot compute**: no expressions are evaluated. The loader (`aeids/knowledge/metrics.py`) refuses the file if a step names an unknown function, a wrong or missing argument, an input or step that does not exist yet, an unused input, an unknown line item, or a bad period rule; constants (365, 1,000,000) become stored typed inputs so every step replays from the `calculations` table. Tests dry-run every recipe and check its output unit.
  - **Calc 1.1**: `average` (periods may differ; the result carries the latest year), `absolute` (outflows printed negative), `intensity` (amount / base × per, in plain units, units may differ: GJ per rupee), `days` (× 365). 1.0 functions unchanged; stored list inputs (`v1`, `v2`, …) now replay by name.
  - **Agents**: Document Intelligence's planning prompt (`plan_v2.md`) carries the catalogue (line items with printed names, metrics with what they need, which BRSR ratios are printed) and asks for the prior year when a metric needs an opening balance; the extraction prompt (`extract_v2.md`) shows each requested line item's printed names. The Financial Analyst (`prompt_v2.md`) sees only the metrics whose line items are among the facts and answers `{metric, inputs: {input: fact id}}`; code checks the binding (exact inputs, one company, same line item for growth, units via the line item: a count read without a unit becomes a count, a currency line item needs a currency amount) and runs the recipe. Every step is a finding (`calculation_step` for intermediate ones, `calculation` for the metric) citing the evidence it depends on. Basic functions remain for questions no metric covers. Prompt versions are `di-v2+kb-1.0` and `fa-v2+kb-1.0` in `agent_runs`.
- Alternatives considered: a reference document only (the model would still write formulas); formulas as expressions evaluated at run time (an injection and correctness risk); Python dicts (not reviewable by analysts).
- Why: one place to define and review metrics, Indian report vocabulary for search and extraction, and multi-step metrics (ROE on average equity, free cash flow, BRSR intensities) that are fixed, tested and auditable instead of improvised by the model.
- Consequences / follow-ups: live on the eval corpus: Apple ROE FY2025 171.42 % (average equity step 65,341.5), Microsoft free cash flow USD 71,611 million and debt-to-equity 0.1256, NVIDIA gross margin 74.99 % / 71.07 % (the report prints 75.0 % / 71.1 %). The corpus has no BRSR report yet: BRSR metrics are covered by tests with synthetic values only; add one or two Indian annual reports with BRSR to the corpus to check extraction. M5 can compare BRSR's printed ratios with the recomputed ones (`reported_ratio_of`). Bump `version` in the YAML whenever a definition changes.

### D-025 — M4 agents: the model points at table cells and picks calculations; values and numbers come from code
- Date: 2026-11-22
- Status: accepted
- Decision:
  - **Calculation module** (`aeids/calc/`, `calc_version` 1.0): pure functions `pct_change`, `difference`, `ratio`, `margin`, `cagr`, `sum_values`, `normalize_scale` on `Quantity` inputs, returning `CalcResult` (function, formula, inputs as given, full-precision output). Scales are aligned to the first input's (units / thousand / lakh / million / crore / billion); refusals are typed errors: currency or unit mismatch, one scale stated and the other not, periods that do not fit (pct_change needs two periods, margin / ratio / sum one, cagr two fiscal years in order), division by zero, missing input. 28 significant digits; rounding only in `display`.
  - **Document Intelligence** (`agents/document_intelligence/`, prompts `plan_v1.md` + `extract_v1.md`, `di-v1`): (1) the model maps the companies named in the question to the tenant's companies with a current report and lists the reported line items and fiscal years needed; a named company with no report stops the task (`needs_more_information`), so AEIDS never answers from another company's data; (2) hybrid search per line item and company, table chunks only; (3) the model answers with cell references (`T2.R5.C1`), never numbers; (4) code resolves each reference in the chunk's normalised table: value, unit, currency, scale and period from ingestion, the quote is the row exactly as written in the chunk, page and box are the cell's. A reference to a missing cell or a cell without a number is dropped with a warning; line items not found become a report limitation. Each value is a `reported_value` finding plus a typed fact for the analyst. Questions about no numbers get cited passages, as in M3.
  - **Financial Analyst** (`agents/financial_analyst/`, `prompt_v1.md`, `fa-v1`): the model returns `{function, inputs: {name: fact id}, title}`; code runs `aeids.calc`, writes the claim (value from `calc.display`; the model's title only if it has no numbers besides fiscal years), stores the calculation and cites every input's evidence. Unknown functions or facts, cross-company combinations (except ratio) and refused calculations are dropped with warnings.
  - **Model replies** (`agents/llm_json.py`): JSON mode, temperature 0, validated with Pydantic; invalid replies are schema errors, retried once with the error in the task's context (`previous_error`), then final. Document text sits in `<document>` blocks that it cannot close (rule 7). Tokens, cost and the served model go to `agent_runs`.
  - **Selection** (`agents/registry.py`): `AEIDS_AGENT_MODE` = `auto` (LLM agents when an OpenRouter key is set, default) / `llm` / `stub`. Verification stays the structural checker until M5. Reports now carry each finding's calculation; `GET /workflows/{id}/findings` too.
- Alternatives considered: the model copying numbers out of the text (unverifiable, the main source of wrong figures); extracting numbers from prose (deferred: numeric facts come only from normalised table cells for now); an LLM planner in the Supervisor (not needed yet: the template plan `extract` → `analyse` covers M4 questions); OpenRouter structured-output JSON schemas (not supported by every provider of the pinned model; Pydantic validation is provider-independent).
- Why: rule 2 and rule 1 hold by construction: the model can only choose among values that exist in the parsed tables, and every number it reports is recomputable from the `calculations` row.
- Consequences / follow-ups: done-when shown live on the eval corpus with `qwen/qwen3.8-flash`: "Compare Apple's revenue and operating margin FY2023–FY2025" → 6 reported values (p. 32 of the 10-K) and 5 calculations (margins 29.82 %, 31.51 %, 31.97 %; growth 2.02 %, 6.43 %), all verified, ~9.7k tokens, USD 0.0024. Also checked live: NVIDIA growth FY2025→FY2026 (65.47 %), Microsoft operating margin FY2025 (45.62 %), Amazon (not uploaded) refused, Tesla supply-chain risks answered with cited passages. Values only in prose or charts are not extracted yet. M5: verification replays calculations and checks each quoted value, page, unit and period.

### D-024 — M3 orchestration: hand-written Supervisor over Postgres state, RQ workers with leases
- Date: 2026-11-21
- Status: accepted (resolves D-003; D-011 accepted as implemented)
- Decision:
  - **Contracts** (`aeids/contracts/`, schema 1.0): `TaskMessage`, `AgentResult`, `Finding`, `Quantity`, `Calculation`, `EvidenceRef`, `EvidenceProposal`, `VerificationDecision`. Unknown fields refused; numbers are `Decimal` (floats refused, strings in JSON); currency amounts need an ISO currency; a finding needs at least one evidence reference; a finding's value equals its calculation's output; error results carry errors and no findings.
  - **State machines** (`aeids/orchestration/states.py`): exactly the task and workflow transitions of ARCHITECTURE.md §3.1; anything else raises `IllegalTransition` and writes nothing. Every task change is a `task_transitions` row and every workflow change an `audit_events` row (reason, actor).
  - **Supervisor** (`supervisor.py`): plans (M3: a template plan `extract` → `analyse`; an LLM planner comes with M4), adds a verification task for every task that produced findings, applies verification outcomes, creates rework tasks (`parent_task_id`, `rework_round` + 1, dependents re-pointed), retries transient errors with exponential backoff, retries schema errors once, never retries permission errors, sends agent-blocked tasks to human review, and builds the report from verified findings only (everything else is a limitation). All of this is idempotent reconciliation (`tick`) under a workflow row lock; unique idempotency keys per task.
  - **Workers** (`runner.py`, `jobs.py`): claim a task with a conditional update (one claim at a time) and a lease of `timeout_seconds`; run the agent outside any transaction; hand the result back. Results are accepted only from the lease holder (fencing). An expired lease returns the task to QUEUED (counted as an attempt). Workers resume unfinished workflows when they start. Agents propose evidence (chunk, page, exact quote); the Supervisor stores it only after checking the chunk belongs to the tenant and the quote is in its text, so nothing is invented or stored twice.
  - **Agents** (`aeids/agents/`): narrow read-only context (tenant-scoped search, evidence lookup), no state changes, no agent-to-agent calls. M3 stand-ins: Document Intelligence (real hybrid search, evidence quotes), Financial Analyst (a summary finding citing the evidence, no numbers yet), Verification (real structural check: cited evidence exists for the tenant and quotes its chunk).
  - **API/console**: `POST/GET /api/v1/workflows`, `/{id}`, `/{id}/tasks`, `/{id}/findings`, `/{id}/cancel`; console page Workflows (live task graph, timeline, findings with evidence, report). Each workflow is one trace: a root span plus a span per task run.
- Alternatives considered: LangGraph (D-003; its checkpointing would duplicate the Postgres state machine and hide transitions), RQ retries for agent errors (retry policy belongs to the Supervisor), letting agents write evidence directly (breaks fencing).
- Why: rules 3–5 and 8 must be owned and auditable; reconciliation makes crash recovery a non-event.
- Consequences / follow-ups: M3 done-when shown with real processes: worker A was `kill -9`ed holding `analyse`; after the 25 s lease worker B re-ran it (attempt 2, "lease recovered"); `extract` (VERIFIED) ran once; one finding, no duplicates. M4 replaces the stand-ins with LLM agents and the calc module; human review endpoints (`/reviews`) arrive with M5.

### D-023 — Repeatable embeddings: pinned provider, cached question vectors
- Date: 2026-11-21
- Status: accepted (fixes the benchmark repeatability follow-up of D-021/D-022)
- Decision: OpenRouter serves `qwen/qwen3-embedding-8b` from three providers (Nebius bf16, DeepInfra, SiliconFlow fp8) whose vectors differ: the same question gave cosine 0.9965 between providers and 0.9999 even within one provider. AEIDS now (1) pins the provider: `AEIDS_EMBEDDING_PROVIDER=nebius` (highest precision, bf16) with `AEIDS_EMBEDDING_ALLOW_FALLBACKS=false`, so documents and questions are always embedded the same way; (2) caches question vectors in Postgres (`query_embeddings`, key = SHA-256 of model, provider and the exact embedded text; migration 0005), so the same question always gets the same vector; (3) rounds every vector to float32 (what Qdrant stores) before returning or caching it, so a fresh vector equals its cached copy; (4) can re-embed an indexed version in place (`reindex_version`: same point ids are overwritten, `is_current` kept, count verified, the document never leaves search; `eval/run_retrieval.py --reindex`). Each embedding span records the provider that answered.
- Alternatives considered: allowing provider fallbacks (silently mixes vector spaces); averaging several calls (costly, still not exact); caching document vectors too (they are embedded once and stored in Qdrant already).
- Why: the benchmark must be repeatable before AEIDS is compared with the baseline (Release 0 definition of done).
- Consequences / follow-ups: after re-embedding the 5-report corpus with Nebius, three runs gave identical ranks for all 93 question/mode pairs: dense R@5 0.61 / R@10 0.81 / MRR 0.53; keyword 0.55 / 0.81 / 0.37; **hybrid 0.77 / 0.90 / 0.53**; m1 and n6 are not found by any mode. If Nebius is down, embedding fails instead of falling back: indexing is retried by the job queue and hybrid search falls back to keyword with a warning. Changing provider or model requires `--reindex` (and, for users' documents, `reindex_version`). The query cache has no expiry yet.

### D-022 — Run tracing in Postgres and a web console at /ui
- Date: 2026-11-20
- Status: accepted (resolves D-009 for now; pulls a small operator console forward from after Release 0, at the user's request)
- Decision:
  - **Tracing** (`aeids/tracing.py`): every operation is a span in a trace, like LangSmith: processing jobs (`process_version` → `tablelens` → `normalise_and_chunk` → `index_version` → `embed_chunks` → `openrouter.embeddings` per batch → `qdrant.replace_version`), searches (`search` → `dense_search` → `keyword_search` → `rrf_fuse`), LLM calls (`openrouter.chat`) and benchmark runs. Spans nest through a context variable (also across the embedding thread pool) and carry kind, status, timing, inputs, outputs, attributes, model, tokens and cost. Ids are OpenTelemetry-shaped (32/16 hex) so traces can be exported to Langfuse/OTel later. Stored in `trace_spans` (migration 0004) by `SqlTraceSink`, each span in its own short transaction; a failing sink never breaks the traced work. Inputs/outputs are JSON-safe, truncated (2,000 chars, 50 items) and key-like strings are redacted. `AEIDS_TRACING_ENABLED` turns storing off.
  - **Console** (`aeids/web/`, served at `/ui`, data from `/api/v1/dashboard/*`): overview (service health, KPIs, pipeline graphic, activity, latency by step, version states), documents (each version's pipeline progress, errors, warnings), search playground (ranks in both lists, link to the trace), evaluation (scores by mode, per-question rank heatmap, run history), traces (list and a waterfall with an inspector for inputs/outputs/attributes) and logs. Plain HTML/CSS/JS, no build step, SVG charts drawn in place; dark and light themes in the brand colours midnight black `#0B0B0C`, pure white `#FFFFFF` and lime green `#C6F432` (lime for fills and single-series charts; in the light theme a dark lime `#4A6600` for accent text, since lime on white is unreadable). Multi-series charts keep palette colours that pass the colour-blindness checks: lime among them is a deeper `#7BA20B`/`#7FA30A`, paired with blue and magenta (orange next to lime failed the deuteranopia check). Status colours (failed red, indexed green) stay reserved and always come with an icon and label. Untrusted text (documents, trace payloads, logs) is only inserted as text.
  - **Uploads from the console**: the Documents page uploads through the main API (`POST /api/v1/documents`, `/{id}/versions`, `/process`), so uploads always land in the default tenant; viewing another tenant shows a note and switches back after an upload. Progress via XHR; the page polls every 3 s while a version uploaded in the last 30 minutes is pending/parsing/indexing.
  - **Logs**: `./start.sh` also writes each program's lines to `logs/<name>.log` (5 MB, one old file kept); the console tails them. Uvicorn access lines for the console's own GET polling are dropped.
- Alternatives considered: Langfuse (another service to run); OpenTelemetry collector + Jaeger (no token/cost view, more moving parts); a React/Vite frontend (a build step for a local console).
- Why: the user asked for a LangSmith-like tracker and a page with results and logs; Postgres is already there, and the `agent_runs` audit of M3 can build on the same spans.
- Consequences / follow-ups: the console reads any tenant and every trace and has no login, like the rest of the API: keep it on 127.0.0.1 until auth exists. No retention for `trace_spans` yet. Agent runs (M3) should record their spans with prompt version and trace id. Benchmark repeatability: two runs with identical document vectors gave hybrid Recall@5 0.81 and 0.74, because OpenRouter returned slightly different query vectors; pinning the provider (or caching query embeddings for the benchmark) is needed before comparing AEIDS with the baseline.

### D-021 — Indexing, search and the retrieval benchmark (M2)
- Date: 2026-11-20
- Status: accepted (details for D-007; completes rule 6)
- Decision:
  - **Indexing** is the last step of the processing job (`aeids/ingestion/indexing.py`): `parsed -> indexing -> indexed | failed`. Chunks are embedded (OpenRouter, 32 per batch, 4 batches in parallel), written to Qdrant (one point per chunk, point id = chunk id, payload as ARCHITECTURE.md §4.1 plus `kind` and `chunk_index`), and the point count is verified before anything points at them. A retry after a successful parse only re-indexes (`failed -> indexing` when `parsed_at` is set) and replaces the version's points.
  - **Versioning** (rule 6): an upload is stored as *not current*. Only after its index is verified does a version become current: Qdrant marks the new points current, Postgres switches the pointer, then the old points are marked not current. A version finishing after a newer one does not take the pointer back.
  - **Collections**: one per embedding model and size (`aeids_chunks__<model>__<dim>`, cosine), with payload indexes on tenant (tenant key), company, document, version, type, `is_current` and `financial_year`.
  - **Search** (`aeids/retrieval/search.py`, `POST /api/v1/search`): modes `dense`, `keyword` and `hybrid` (default). Keyword search is Postgres full-text search on section + text (`english`), with the question's lexemes OR-ed and quoted (no user text reaches the tsquery parser unquoted) and ranked by `ts_rank`. Hybrid is RRF with k = 60 over the top 50 of each list. Every hit is re-checked in Postgres (tenant, current version, `indexed` status, active document, filters), so a stale Qdrant payload never leaks a superseded or foreign chunk. If dense search is down, hybrid falls back to keyword with a warning; `dense` alone returns 503. SQLite (tests) uses a small BM25 scorer instead of full-text search.
  - **Benchmark** (`eval/run_retrieval.py`): the five reports of D-008 in a separate `eval` tenant, loaded from their existing TableLens output; 31 questions in `eval/retrieval_questions.jsonl`. Gold pages are the pages whose text matches the question's `evidence` pattern (the printed answer), and `check-gold` re-derives them. Metrics: Recall@5 and Recall@10 (share of questions with a gold page of the right report in the top K) and MRR@10.
- Alternatives considered: promote on upload (the M1 rule); `websearch_to_tsquery` (AND semantics: long questions match nothing); one shared collection for all models; hand-picked gold pages.
- Why: rule 6 and rule 10; OR-ed lexemes keep keyword recall on natural-language questions; pattern-derived gold pages can be checked by anyone.
- Consequences / follow-ups: first run (2026-11-20, `qwen/qwen3-embedding-8b`, 2,030 chunks, embedding cost USD 0.0056): dense R@5 0.61 / R@10 0.84 / MRR 0.50; keyword 0.55 / 0.81 / 0.37; **hybrid 0.81 / 0.90 / 0.51**. Missed by all modes: NVIDIA's employee count (the right chunk exists, but many chunks about employee stock plans in the exhibits and proxy outrank it). Candidates for later: a reranker, company/fiscal-year filters from the question, section-heading weights. Re-parsing an `indexed` version is not supported yet (its points would need removing first).

### D-020 — TableLens code copied into this repo (`tablelens/`)
- Date: 2026-11-20
- Status: accepted (amends D-015: one project folder instead of two)
- Decision: the TableLens code from `../IntelligencePDF` is copied unchanged into `tablelens/`: every Python file (including `medical.py`, `reference.py` and `spotting_check.py`, which `app.py` and `gpu_server.py` import), `index.html`, its own `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `README.md`, and the fine-tuned Table Transformer `models/tatr-finetuned-01-11` (Git LFS, `.gitattributes`). Left out: `doc/`, `output/`, the virtual env, caches, `.git` history, and the old AEIDS docs. `tablelens/doc/` and `tablelens/output/` are git-ignored (rule 11).
- Alternatives considered: keep two folders (D-015); turn TableLens into an importable package that the worker calls in-process.
- Why: chosen by the user. Running and maintaining two folders was double work.
- Consequences / follow-ups: TableLens still runs as its own process (`cd tablelens && python app.py`, port 8000) with its own Python 3.11 env (`tablelens/.venv`), because torch/paddle stay out of the AEIDS env (D-014). AEIDS still talks to it over HTTP (D-019), so `aeids/` imports nothing from `tablelens/`, and medical stays outside AEIDS (D-010). Edit TableLens in `tablelens/` from now on; `../IntelligencePDF` is the old copy and holds the git history (`git show 581640e:reference.py`) and the existing outputs of the 5-company corpus. `test_recognise.py` already fails in the original (its stub page has no `get_texttrace`); the start-up self-checks pass.

### D-019 — Uploads are parsed automatically by calling the TableLens server
- Date: 2026-11-20
- Status: accepted (the user chose this over a manual import command; extends D-017)
- Decision: a new upload (or version) queues an RQ job (`process-<version_id>`) that sends the stored PDF to the TableLens server (IntelligencePDF `app.py`, `AEIDS_TABLELENS_URL`, default `http://127.0.0.1:8000`): `POST /detect`, then `POST /recognise` with `mode=all`, polling `GET /events/{job}` every ≤ 10 s (TableLens drops jobs not polled for 60 s), then `GET /output/<stem>/structure/<stem>_structure.json`. The result goes through the same validation/normalisation/chunking as an attached file. When TableLens' Modal GPU server is deployed, `app.py` forwards the work there itself; AEIDS never talks to Modal. TableLens stays a separate project; no change was needed there.
- Naming: the PDF is sent as `<file stem>-<sha256[:12]>.pdf`, so a replacement version with the same file name never gets an old result; an existing TableLens result for that name is reused (a retry never re-runs GPU work). `reuse_by_filename=true` (process endpoint) reuses a result made earlier under the plain file name, checked by page count.
- Status and retries: `parser_status` is `parsing` for the whole TableLens run. TableLens down/restarted/timeouts are retried by RQ (60 s, 5 min, 15 min); a TableLens job error, a 4xx, or an invalid/mismatched structure marks the version `failed` with the reason, not retried. `POST …/versions/{id}/process` re-runs; `python -m aeids.worker requeue` re-queues versions left pending/failed/stuck by a lost worker or queue outage. An upload succeeds even when Redis is down (`processing: not_queued`).
- Alternatives considered: manual attach / an import command for TableLens output folders (option 2); importing TableLens in-process.
- Consequences: AEIDS uploads appear in TableLens' `doc/` and `output/` under the suffixed names. TableLens runs one job at a time, so large batches queue there. TableLens' medical reference-JSON step logs an error on financial reports; AEIDS ignores that message. The worker runs RQ `SimpleWorker` on macOS (fork safety).

### D-018 — Section-aware chunking
- Date: 2026-11-20
- Status: accepted (chunking version 2)
- Decision: headings form a section tree — level 0 `PART I–IV`, 1 `Item 1A.` / `ITEM 7.`, 2 financial statement titles, `Note N –/—/.` and other ALL-CAPS headings (glossy reports such as Microsoft's), 3 any other short heading; a sentence-like "heading" (≥ 12 words, > 120 chars, or a full sentence) is read as a paragraph (10-K risk-factor leads). Every chunk carries its path (`PART II > Item 7. … > Segment Operating Performance`) in `section`, in `meta.section_path`, and as the first line of its text (`Section: …`) so dense and keyword search both see it. Text never crosses a section, except that a subsection under 400 characters merges with its next sibling under their common parent (`meta.subsections`). Text of up to 400 characters right before a table in the same section becomes the table chunk's lead-in.
- Alternatives considered: flat "last heading" sections (version 1); fixed-size windows with overlap.
- Why: on the five-report corpus, version 1 produced many one-sentence sections (risk factors) and table chunks without their caption; version 2 cut Apple from 423 to 290 chunks (text median ~1,000 chars) and gives 20–39 tables per report their lead-in sentence.
- Consequences: ALL-CAPS headings are all level 2, so in glossy reports a caps subsection replaces its caps parent instead of nesting. Changing chunking means re-indexing (`chunking_version` is stored on each chunk).

### D-008 — Benchmark corpus: five US big-tech annual reports
- Date: 2026-11-20
- Status: accepted
- Decision: Apple FY2025 10-K, Microsoft FY2025 10-K, NVIDIA FY2026 10-K, Tesla FY2025 10-K, Alphabet annual report (`goog014907-ars`, fiscal years 2023–2025). PDFs in `../IntelligencePDF/doc/`, TableLens output in `../IntelligencePDF/output/`; not in git — `eval/corpus.csv` will record file, fiscal year, SHA-256 and source URL.
- Why: chosen by the user; public filings; all five parse (tables 47–112 per report, revenue and net income read with USD/million/fiscal year correctly).
- Consequences / follow-ups: Alphabet prints years oldest-first, a useful check that periods come from headers, not column position. `eval/corpus.csv` lists file, company, fiscal year, SHA-256 and page count (D-021); the source URLs are still to be filled in.

### D-007 — Hybrid retrieval: Qdrant dense + Postgres full-text, fused with RRF
- Date: 2026-11-20
- Status: accepted
- Decision: dense search in Qdrant; keyword search with Postgres full-text search on chunk text and section; results combined with reciprocal rank fusion. Every query filters by `tenant_id` and, by default, `is_current`.
- Alternatives considered: Qdrant sparse vectors (BM25/SPLADE).
- Why: no extra model or service; Postgres is already the source of truth for chunks.

### D-004 — Background worker: RQ on Redis
- Date: 2026-11-20
- Status: accepted
- Decision: RQ (`rq==3.0.0`) on the compose Redis for indexing jobs, with bounded retries for transient errors.
- Alternatives considered: Celery, Arq, Dramatiq.
- Why: simplest option for local Docker Compose; Redis is already part of the stack. Task-state durability stays in Postgres (rule 5), not in the queue.

### D-002 — Embeddings: qwen/qwen3-embedding-8b via OpenRouter
- Date: 2026-11-20
- Status: accepted (replaces the local-model suggestion)
- Decision: `qwen/qwen3-embedding-8b` through OpenRouter's `/embeddings` endpoint ($0.01 per million tokens, 40,960-token context on 2026-11-20). Questions are embedded with a task instruction (`Instruct: …\nQuery: …`), documents without. The vector size is taken from the first response and fixed per Qdrant collection; the collection name includes the model, so a model change means a new collection and a re-index.
- Alternatives considered: local BAAI/bge-small / bge-base via fastembed (measured 4–17 chunks/s on this Mac's CPU: 3–12 minutes for the corpus).
- Why: chosen by the user; much stronger model at negligible cost (the whole corpus is about 1M tokens).
- Consequences: chunk text is sent to OpenRouter. Acceptable for public filings; private documents would need a different setting. Needs `AEIDS_OPENROUTER_API_KEY`.

### D-001 — LLM: qwen/qwen3.8-flash via OpenRouter
- Date: 2026-11-20
- Status: accepted
- Decision: `qwen/qwen3.8-flash` through OpenRouter chat completions ($0.15 / $0.47 per million input / output tokens, 1M context on 2026-11-20; supports `response_format`, structured outputs and tools). Calls use temperature 0, request usage accounting, and go through `aeids/llm/openrouter.py`, which retries only transient errors (timeouts, 408/425/429/5xx) with exponential backoff and never retries 401/402/403 or bad requests. The key comes only from `AEIDS_OPENROUTER_API_KEY` and is scrubbed from error messages.
- Why: chosen by the user; cheap enough for repeated benchmark runs.
- Consequences: the single-agent RAG baseline (M6) uses the same model, so the comparison is fair. `python -m aeids.llm check` verifies the setup.

### D-017 — TableLens handoff: AEIDS reads `_structure.json`; financial normalisation rules
- Date: 2026-11-20
- Status: accepted
- Decision: TableLens runs on its own (local or Modal GPU) and writes `output/<name>/structure/<name>_structure.json`; the user chose that AEIDS reads these files. A file is attached to a stored version with `POST /api/v1/documents/{id}/versions/{vid}/structure` or `python -m aeids.ingestion attach`; `python -m aeids.ingestion inspect` shows the normalised tables without a database. On attach: schema validation (untrusted input), page numbers checked against the stored PDF (pypdf; a structure referring to a page the PDF lacks is rejected), file-name mismatch is a warning, the JSON is stored as a derived artifact (`<tenant>/<sha[:2]>/<sha>.json`), chunks replace any earlier chunks of the version. `parser_status` moves only `pending→parsing→parsed→indexing→indexed`, `parsing/indexing→failed`, `failed/parsed→parsing`; re-parsing an `indexed` version is refused until indexing can remove its vectors.
- Normalisation (port of `reference.py` @581640e, checked on Apple's FY2025 10-K): `Decimal` values; unit / ISO currency / scale separate; Indian lakh grouping, `Rs.`, `Cr`, `Lacs`; unit from column header → title → subtitle → up to 3 texts before the table on its page → the previous table's caption on that page; "except" clauses give per-share rows/columns scale `units` and share counts unit `count` with the stated scale; a label wrapped over two rows is joined; unflagged second header lines join the header; `period` = `FY<year>` only when a header names exactly one year (date, `FY25`, `2024-25`, bare year); a total row closes its section. Each cell records `scale_source`. Charts read by PaddleOCR-VL are chunked but flagged `image_read`.
- Alternatives considered: AEIDS calling the TableLens server per PDF; importing TableLens in-process (torch/paddle in AEIDS).
- Why: TableLens output already exists per document and is inspectable; no GPU stack in AEIDS (D-015).
- Consequences / follow-ups: label indent levels need PDF words, which `_structure.json` lacks (`level` is None). Wrapped labels and unflagged header lines would be better fixed in TableLens itself. Ingestion runs in the request until the worker (D-004) exists.

### D-016 — Upload dedupe and re-upload semantics
- Date: 2026-11-20
- Status: accepted
- Decision: `POST /documents` with bytes whose SHA-256 already exists anywhere in the tenant returns that existing version (HTTP 200, `duplicate: true`) and creates nothing. `POST /documents/{id}/versions` with bytes matching an existing version of *that* document returns it (200) and leaves the current pointer unchanged — re-uploading an old version does not roll back. The same bytes in another tenant are a new document. New versions get `max(version_number) + 1` under a row lock on the document. Original filenames are display-only (sanitised basename); storage keys come from the checksum.
- Alternatives considered: re-upload of an old version makes it current again; dedupe only within a document.
- Why: keeps uploads idempotent for retries and avoids two logical documents for one file; an explicit rollback can be a separate API later (`promote_to_current` already supports it).

### D-015 — AEIDS is a standalone repo; TableLens stays in its own repo
- Date: 2026-11-20
- Status: accepted, amended by D-020 (TableLens code now copied into `tablelens/`) (supersedes the file naming in D-014; amends D-006's "in this repo")
- Decision: AEIDS lives in `Desktop/saurabh/AEIDS` with its own git repo, `requirements.txt` and `docker-compose.yml`. TableLens stays in `../IntelligencePDF` (GitHub `PDF-Document-Parser`). AEIDS ingestion consumes TableLens output (`_structure.json`) through an interface in `aeids/ingestion/`; the exact handoff is decided at the start of M2 with the user, who built the parsing part.
- Alternatives considered: `aeids/` package inside the TableLens repo (the original plan, D-014).
- Why: chosen by the user; keeps torch/paddle and the GPU image out of the AEIDS environment, and lets each repo be versioned and deployed on its own.
- Consequences / follow-ups: the first M1 build (inside IntelligencePDF, never committed) was lost and rebuilt here. The `intelligencepdf` Docker Compose project's Postgres/Qdrant/Redis bind the same ports as `docker-compose.yml`; stop one before starting the other.

### D-014 — AEIDS dependencies and services kept apart from TableLens
- Date: 2026-11-20
- Status: superseded by D-015 (file names); the separation itself still holds
- Decision: AEIDS pins live in `requirements-aeids.txt` and its services (Postgres 18, Qdrant 1.20.2, Redis 8.8) in `docker-compose.aeids.yml`; the AEIDS API runs on port 8001. `requirements.txt` and `docker-compose.yml` stay TableLens-only.
- Alternatives considered: one requirements file and one compose file for both.
- Why: the TableLens Docker image and Modal server install `requirements.txt`; adding the web/DB stack there would grow them for nothing, and the GPU container cannot start on a laptop without NVIDIA.
- Consequences / follow-ups: M2 decides how the ingestion worker reaches TableLens (in-process import vs the GPU server).

### D-013 — Python 3.11
- Date: 2026-11-20
- Status: accepted
- Decision: Python 3.11 for the whole repo (`.venv` built with `uv venv --python 3.11`).
- Why: the existing pins (numpy 1.26.4, torch 2.9.1, paddlepaddle 3.2.1) target 3.11; numpy 1.26 has no wheels for newer Pythons.

### D-005 — Object storage
- Date: 2026-11-20
- Status: accepted
- Decision: local filesystem behind the `ObjectStore` interface (`aeids/storage.py`), content-addressed keys `<tenant>/<sha[:2]>/<sha>.pdf`, URIs `local://<key>`.
- Alternatives considered: MinIO (S3-compatible) from the start.
- Why: one less service for Release 0; content-addressed keys make storing idempotent and deduplicate identical files.
- Consequences / follow-ups: a MinIO store implements the same `put_file`; uploads still stage on local disk first.

### D-010 — AEIDS is financial-report only; the medical lab-report work is separate
- Date: 2026-11-20
- Status: accepted
- Decision: AEIDS analyses company financial reports (annual reports, ESG/BRSR). The medical lab-report feature (`medical.py`, `reference.py` with schema `medical-report/1.0`) is a separate TableLens feature, outside AEIDS scope, and is left as it is. AEIDS ingestion consumes the generic recognition output (`output/<name>/structure/<name>_structure.json`) and does its own financial normalisation in `aeids/ingestion/`, ported from the financial `reference.py` in commit `581640e` (currency + scale such as "EUR million" / lakh / crore, periods from column headers, signed negatives, total rows, indent levels).
- Alternatives considered: generalising `reference.py` to serve both; re-targeting AEIDS at medical reports.
- Why: the user confirmed medical is not part of AEIDS. The recognition output is domain-neutral, and the pre-medical `reference.py` already solved the financial normalisation, so nothing medical needs to change.
- Consequences / follow-ups: `aeids/` never imports `medical.py` or the medical reference JSON; the TableLens self-checks must keep passing; the financial normaliser gets its own tests (port the `581640e` self-check cases).

### D-000 — Release 0 scope
- Date: 2026-11-20
- Status: accepted
- Decision: Build Supervisor + Document Intelligence + Financial Analyst + Verification, plus a single-agent RAG baseline and benchmark, before adding Research, Regulatory or Risk agents.
- Why: Proves the core claim (verified, evidence-grounded answers beat a simple RAG baseline) with the smallest system that can be finished and measured.
