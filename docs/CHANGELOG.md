# AEIDS changelog

What each Release 0 milestone added, newest last. Design decisions are in [DECISIONS.md](DECISIONS.md).


**What works today:** uploading reports, versioning, automatic parsing, hybrid search, the Supervisor and its task plan, crash-safe workers, citation verification, tracing and the web console.

**New in M4:** Document Intelligence reads reported values from the parsed tables, and the Financial Analyst calculates margins and growth with the calculation module. For example, *"Compare Apple's revenue and operating margin FY2023–FY2025"* on Apple's 10-K gives 6 reported values (page 32) and 5 calculations (operating margin 29.82% → 31.51% → 31.97%), all verified, for about USD 0.002.

**Metric knowledge base:** [`aeids/knowledge/metrics.yaml`](../aeids/knowledge/metrics.yaml) defines 41 metrics (margins, growth, ROE, free cash flow, leverage, BRSR intensities, women's representation, LTIFR, CSR spend) as fixed recipes of tested calculation steps, plus how US GAAP, Ind AS and BRSR reports name each line item. The agents pick a metric and the facts; code runs the recipe.

**New in M5:** Verification replays every calculation and checks every value against the table cell it came from (value, page, unit, currency, scale, period, current version). Wrong findings go back for at most two rework rounds; what still fails is dropped and listed as a limitation; what cannot be checked automatically waits for a person in the console.

**New in M6:** a single-agent RAG baseline and one command that benchmarks both on 37 questions: AEIDS answers 89–92% correctly (over several runs) vs 68–69%, with no unchecked numbers (baseline: 12–14%), at a median under 5 s and under USD 0.001 per question ([results](../README.md#does-aeids-beat-one-rag-prompt)).

**New in M7:** every completed question gets a stored, checked report (JSON and Markdown): the answer, every calculation with its inputs, numbered sources down to the page and evidence record, limitations, review status and how it was made. Before it is stored, code checks that it contains every verified finding and nothing else. A report that fails the check is not stored, and the workflow fails instead. Download it from the answer page, `GET /api/v1/workflows/{id}/report.md`, or `python -m aeids.reporting <id>`.

**Since the Release 0 review:**

- checks you can trust: an upload must be the type it says it is, and ESG questions are answered only from ESG reports or BRSRs (D-035);
- a question about a company with no uploaded report is refused, for quoted answers too; countries and regulators ("US export controls") are not mistaken for companies (D-036);
- a local-only API guard and safer secrets (D-034);
- earnings per share in the knowledge base (D-033);
- a simpler console: mint and amber, a hideable history panel, illustrations.

**Deployment and demo:**

- a demo deployment on Render: migrations on start, inline processing, Qdrant Cloud with an API key, TableLens reached over a tunnel (D-037);
- delete a report: archived, so past answers keep their sources, and the same PDF can be uploaded again (D-038);
- a read-only demo mode for public links (D-039), with an owner key that unlocks editing for the owner's browser tab only (D-040).

**Not yet:** values printed only in prose or charts are not extracted, and ESG/BRSR reports are not yet in the benchmark.
