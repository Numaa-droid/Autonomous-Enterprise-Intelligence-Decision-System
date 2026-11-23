# Release 0 review

Date: 2026-11-23 · Scope: [MVP_PLAN.md](MVP_PLAN.md) §6 checklist, [ARCHITECTURE.md](ARCHITECTURE.md) §9 definition of done, and the 11 non-negotiable rules in `CLAUDE.md`.

**Verdict: Release 0 is done.** Every item on the Release 0 checklist is met and backed by an automated test, a check against the live system, or both. The review found three defects, all fixed with regression tests (see [Defects found and fixed](#defects-found-and-fixed)). Some gaps are left open. None of them blocks Release 0, but two should be closed before Release 1 work starts (see [Open items](#open-items)).

## How the review was done

- **Tests:** 472 tests, all passing on SQLite and on Postgres 18. They use a fake model and an in-memory vector index, and never call the network.
- **Live database audit:** a read-only script over the development database:
  - 269 workflows, 5,843 task transitions and 1,068 workflow transitions;
  - 162 stored calculations and 594 findings;
  - 6,566 trace records.
  Report checks ran inside a transaction that was then rolled back.
- **Live crash test:** a worker was killed with `kill -9` in the middle of a task, then restarted.
- **Repeatability:** the full benchmark was run again and compared question by question with the earlier run.
- **Static checks:** the code was checked against each non-negotiable rule.

## Release 0 checklist

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | Multiple reports uploaded; versions and processing status visible | ✅ Met | Live: 6 reports in 2 workspaces, all `indexed`, exactly one current version per document. Console **Reports** page shows each version's status. Tests: `test_documents_api` (15), `test_documents_service` (14), `test_tablelens` (18). |
| 2 | Financial question → visible task plan with dependencies | ✅ Met | The plan is `extract → analyse → summarise`, with a `verify` task added for each task that has findings. It is visible in the console ("How AEIDS got this answer") and at `GET /workflows/{id}/tasks`. A task is queued only when every dependency is verified. Tests: `test_workflows` (32). |
| 3 | Schema-valid agent outputs with evidence IDs | ✅ Met | Agents exchange Pydantic `TaskMessage` / `AgentResult` objects. A finding without evidence is refused by the contract. Live: 0 of 594 findings lack evidence, and all 870 verified quotes are found in their source chunk. A schema error is retried once (`test_schema_errors_are_retried_once`). |
| 4 | Deterministic, reproducible calculations with tests | ✅ Met | Live: all 162 stored calculations replay to exactly the stored output, including 12 made with the older calc 1.0. Tests: `test_calc` (27), `test_knowledge` (64). |
| 5 | Verification catches all injected faults; bounded rework works | ✅ Met | Faults injected in `test_verification_m5` (22): wrong number, page, version, unit, scale, currency, period, citation, claim text, calculation output, inputs. Each is rejected with its reason and fixed by rework. A fault that persists is dropped after 2 rounds and listed as a limitation. |
| 6 | Report contains only verified findings, citations and limitations | ✅ Met (after fixes) | Before storing, `aeids/reporting/report.py` re-checks every line, source, calculation and summary number against the database. Live: all 260 completed workflows' reports pass that check (after defect 1 was fixed; 4 failed before). Tests: `test_report_m7` (15). |
| 7 | Resume after worker restart without duplicated work | ✅ Met | Live crash test: worker killed during `extract`. When the lease expired (180 s), a new worker took the task as attempt 2, and the workflow completed. Each task completed exactly once, with no duplicate findings. Across the whole database: 0 idempotency keys used twice, 0 tasks completed twice, 0 duplicate claims. Tests: `test_killed_worker_is_recovered_without_duplicate_work`, `test_a_stale_worker_cannot_write_after_losing_its_lease`. |
| 8 | Benchmark vs single-agent baseline runs from one command | ✅ Met | `.venv/bin/python eval/run_benchmark.py`. Three runs, results below. Runs 1 and 2 gave the same 36 verdicts for both systems, with identical AEIDS answer text on 35 of 36 questions (baseline: 13 of 36). Run 3 differs on one AEIDS lookup (open item 2). |
| 9 | README with diagram, benchmark table and demo | ✅ Met | README: pipeline, agents and trust diagrams; retrieval and end-to-end benchmark tables; a five-minute demo. |

### Benchmark (run on the fixed code)

Three full runs of the 36 questions. Run 1 is before M7, run 2 is after fixes 1 and 2, run 3 is after fix 3.

| | AEIDS run 1 | AEIDS run 2 | AEIDS run 3 | Baseline (all 3 runs) |
|---|---:|---:|---:|---:|
| Answers correct | 92% | 92% | 89% | 69% |
| &nbsp;&nbsp;lookups (14) | 79% | 79% | 71% | 79% |
| &nbsp;&nbsp;calculations (10) | 100% | 100% | 100% | 80% |
| &nbsp;&nbsp;comparisons (6) | 100% | 100% | 100% | 0% |
| &nbsp;&nbsp;not in the reports (6) | 100% | 100% | 100% | 100% |
| Answers with a correct citation | 93% | 93% | 93% | 63–67% |
| Numbers not printed on a cited page | 0% of 65 | 0% of 65 | 0% of 64 | 12–15% |
| Time per question, median / p95 | 4.6 / 10.5 s | 4.2 / 10.1 s | 3.9 / 7.2 s | 1.7–2.0 / 2.2–2.8 s |
| Cost per question | USD 0.0004 | USD 0.0005 | USD 0.0006 | USD 0.0002–0.0004 |

How the runs compare:
- **Runs 1 and 2:** every verdict is the same for both systems.
- **Runs 2 and 3:** one AEIDS verdict differs. l03 asks for Apple's diluted earnings per share. In run 3 the planning model decided the question asked for no reported number, and AEIDS answered with quoted passages instead of the figure. Earnings per share is not in the knowledge base, so the safeguard that searches for any line item the question names could not catch it (open item 2).
- **The defect 3 fix** changed no verdict, because no benchmark question exercises it (open item 2).

Two things are stable in every run: AEIDS states no number that is not printed on a page it cites, and it never errors.

## The full definition of done (ARCHITECTURE §9)

Where the architecture's wording asks for more than the MVP plan:

- **"A complex financial + ESG question produces a visible task plan": ⚠️ financial only.** The knowledge base defines the BRSR metrics (energy and emission intensities, LTIFR, women's representation, CSR spend), and they are tested. But the corpus is five US annual reports with no BRSR or ESG report, so no ESG question has been run end to end. See open item 1.
- **"The Supervisor selects agents dynamically": ⚠️ partly.** The plan is a fixed sequence of three agents. What changes per question: the analyst makes no model call when the question asks for no calculation; the summary falls back to a template; verification tasks are added only for tasks with findings; and the number and kind of rework tasks follow the verification results. Choosing among many agents only makes sense once Release 1 adds the Research, Regulatory and Risk agents.
- **Everything else in §9 matches the checklist above.**

## Non-negotiable rules

| Rule | Status | Evidence |
|---|---|---|
| 1 Evidence for every claim | ✅ | 0 findings without evidence; verified quotes are all found in their chunks; the report check refuses a line without a source. |
| 2 No LLM arithmetic | ✅ | Every number comes from `aeids/calc/` and is stored with function, formula, inputs and calc version. The summary's numbers are checked. The model only points at table cells and picks recipes. |
| 3 Typed contracts only | ✅ | Agents never import one another; they exchange only `TaskMessage` / `AgentResult`. |
| 4 Only the Supervisor changes state | ✅ | Workers only claim and release tasks; the lease is fenced (`test_a_stale_worker_cannot_write_after_losing_its_lease`). |
| 5 Persisted, legal transitions; resume | ✅ | Live: 5,843 task and 1,068 workflow transitions, none outside the transition tables. Resume: checklist item 7. |
| 6 Explicit document versions | ✅ | Search defaults to current versions only, and a version becomes current only after its index is verified (D-021). Every source in a report shows its version and whether it is still current. |
| 7 Document text is untrusted | ✅ | Text from documents reaches all 4 prompts that use it (Document Intelligence, Financial Analyst, report writer, baseline) only wrapped as untrusted data. The report escapes document text, so it cannot become a link, image or HTML (tested with hostile text). |
| 8 Bounded retries | ✅ | Model calls: at most 3 retries, transient errors only. Tasks: at most 3 attempts with doubling backoff. Permission errors are never retried; schema errors are retried once (tested). |
| 9 Secrets only from the environment | ✅ | No key in the code, the logs or any of the 6,566 stored trace records. The one match in the repo is a fake key in a redaction test. |
| 10 Tenant filter on every vector query | ✅ | Every Qdrant call in `aeids/retrieval/vector_index.py` takes `tenant_id` and filters on it; search results are re-checked against the tenant in Postgres. |
| 11 No documents or outputs in git | ✅ | `git ls-files` lists no PDF, `output/`, `doc/`, `reports/` or `.env`. The corpus is referenced by checksum in `eval/corpus.csv`. |

## Defects found and fixed

| # | Defect | Effect | Fix | Regression test |
|---|---|---|---|---|
| 1 | The report check treated the passage count in the code-written template summary ("… in 5 passages …") as an unchecked number. | Every answer made only of quoted passages, after its AI summary was withheld, ended **FAILED**. It happened live on the Microsoft headcount question. | A template summary must equal exactly what code writes from the verified findings, which is stricter than the number check. AI summaries keep the number check. | `test_a_passages_answer_with_the_template_summary_completes`, `test_check_holds_a_template_summary_to_what_code_writes` |
| 2 | Asking for the report of a workflow completed before M7 whose stored content fails the check raised an unhandled error. | HTTP 500 instead of an answer. | Returns 422 `report_check_failed` with the reasons, and stores nothing. | `test_an_older_workflow_whose_report_fails_the_check_gets_a_clear_error` |
| 3 | Tesla prints operating income as "Income from operations", a label the knowledge base also lists as an Ind AS name for revenue. Document Intelligence correctly extracted it as operating income, but the Financial Analyst's label rule refused it. The refused calculation was not reported anywhere. | *"What was Tesla's operating margin in fiscal year 2025?"* returned only the two inputs, and the report said **Complete** with no limitations. | (a) An ambiguous label counts for a line item only when it is one of that item's names *and* the fact was extracted for that item. (b) A calculation the analyst was asked for but refused is listed as "Could not be calculated from the verified figures: …", making the report **partial**. Live after the fix: Tesla operating margin 4.59%, both inputs cited. | `test_an_ambiguous_label_counts_for_the_line_item_it_was_extracted_for`, `test_a_refused_calculation_is_a_limitation_not_a_silent_gap` |

Each regression test was confirmed to fail on the code before its fix.

## Open items

None of these blocks Release 0. Items 1 and 2 should be closed before Release 1 work starts.

1. **No ESG / BRSR document in the corpus.** The BRSR side of the knowledge base is tested only on synthetic data. Add 1–2 public BRSR reports (an Indian listed company, Ind AS) and a few ESG questions to `eval/questions.jsonl`. They would also exercise Ind AS labels such as "Revenue from operations" and "Income from operations".
2. **Gaps in benchmark coverage and in the knowledge base.**
   - The benchmark did not catch defect 3: no question's calculation depends on a label shared by two line items. Add such a question (for example Tesla's operating margin).
   - l03 (Apple's diluted earnings per share) was right in 2 of 3 runs. Earnings per share is not in the knowledge base, so when the planning model drops it, nothing searches for it.
   - Fix: add basic and diluted EPS to the knowledge base. Their rows are usually labelled just "Basic" / "Diluted", which is also the label of the share-count rows, so the model would pick the cell rather than code. Then rerun the benchmark.
   - Until that is done, report AEIDS accuracy as **89–92%**, not 92%.
3. **Values printed only in prose are not extracted.** For example, employee headcounts. These are the 3 questions AEIDS gets wrong, and it answers them with quoted passages rather than a number.
4. **Values read from charts need a person.** By design (D-027); the review queue handles them.
5. **Crash recovery waits for the lease.** A crashed task resumes only after `AEIDS_TASK_TIMEOUT_SECONDS` (180 s by default), and the length is fixed when the task is planned. That is acceptable for Release 0; a heartbeat would shorten it.
6. **Plan-scope items still deferred:** authentication and real multi-tenancy (`tenant_id` is filtered everywhere; there is one default tenant), PDF report styling, Kubernetes, and cost controls beyond token and cost logging.
7. **Housekeeping:**
   - 10 agent runs from 2026-11-20 have no prompt version. They are M3 stand-in agents that call no model, so this is correct.
   - TableLens's `test_recognise.py` is stale (noted in `CLAUDE.md`).

## Reproducing this review

```bash
.venv/bin/pytest                                                         # 472 tests (SQLite)
AEIDS_TEST_DATABASE_URL=postgresql+psycopg://aeids:<pw>@127.0.0.1:5432/aeids_test .venv/bin/pytest
.venv/bin/python eval/run_benchmark.py                                   # AEIDS vs baseline, 36 questions
.venv/bin/python eval/run_benchmark.py rescore reports/benchmark-<time>.json
```
