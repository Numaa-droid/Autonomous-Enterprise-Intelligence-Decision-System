"""AEIDS against the single-agent RAG baseline on ``eval/questions.jsonl`` (MVP_PLAN.md M6, D-029).

    .venv/bin/python eval/run_benchmark.py                    # both systems, every question
    .venv/bin/python eval/run_benchmark.py --ids l01 c02 m04  # some questions
    .venv/bin/python eval/run_benchmark.py --systems baseline # one system
    .venv/bin/python eval/run_benchmark.py rescore reports/benchmark-<time>.json
    .venv/bin/python eval/run_benchmark.py check-gold         # re-derive gold pages (no services)

What it does:
1. Makes sure the 5-company corpus (``eval/corpus.csv``) is loaded and indexed in the ``eval``
   tenant, as ``run_retrieval.py`` does (steps already done are skipped).
2. Asks every question to both systems over all five reports, without company filters:
   * **AEIDS**: a workflow (Supervisor, Document Intelligence, Financial Analyst, Verification,
     report writer) run to the end in this process; its answer is the verified findings of the
     report. A workflow that stops for human review counts as no answer.
   * **Baseline** (``aeids.baseline``): the same hybrid search and the same chat model, one prompt,
     an answer with cited passages; the model does any arithmetic itself.
3. Scores both with the same rules (``aeids.evaluation.scoring``): answer accuracy within
   tolerance, citation correctness against the gold pages, unsupported-claim rate, latency, and
   tokens and cost per question (summed over each run's trace: every model and embedding call).

Gold pages: each expected value rests on figures printed in a report; ``evidence`` lists them as
patterns, and a figure's gold pages are the pages of its report whose text matches the pattern.
``check-gold`` re-derives them from the TableLens output, so anyone can check them.

Results: a table on screen, and ``reports/benchmark-<time>.json`` (every answer, claim, citation
and score; git-ignored) and ``.md`` (the table). ``rescore`` scores a saved run again without
asking the systems (after a change to the scoring rules).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from run_retrieval import load_corpus, page_texts, prepare, structure_path  # noqa: E402

from aeids.evaluation.scoring import (  # noqa: E402
    Citation,
    Expected,
    Question,
    QuestionScore,
    SystemAnswer,
    mentions,
    score,
    summarise,
)

QUESTIONS = ROOT / "eval" / "questions.jsonl"
SYSTEMS = ("aeids", "baseline")
NAMES = {"aeids": "AEIDS", "baseline": "Baseline (RAG)"}
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


# --- questions and gold pages -------------------------------------------------------------

def load_questions() -> list[dict]:
    return [json.loads(line) for line in QUESTIONS.read_text().splitlines() if line.strip()]


def to_question(q: dict, files: dict[str, str]) -> Question:
    gold: dict[str, set[int]] = {}
    for e in q["evidence"]:
        gold.setdefault(files[e["doc"]], set()).update(e["pages"])
    return Question(q["id"], q["type"], q["question"], [Expected.from_json(x) for x in q["expected"]], gold)


class Pages:
    """Page text of every corpus report, by file name (from the TableLens output)."""

    def __init__(self) -> None:
        self.texts = {r.file: page_texts(json.loads(structure_path(r).read_text())) for r in load_corpus()}

    def __call__(self, file: str, page: int) -> str:
        return self.texts.get(file, {}).get(page, "")


def check_gold() -> int:
    corpus = {r.key: r for r in load_corpus()}
    pages = Pages()
    bad = 0
    for q in load_questions():
        for e in q["evidence"]:
            texts = pages.texts[corpus[e["doc"]].file]
            derived = sorted(p for p, t in texts.items() if re.search(e["pattern"], t, re.I))
            ok = derived == e["pages"] and derived
            bad += not ok
            if not ok:
                print(f"BAD {q['id']} {e['doc']} {e['pattern']!r}: file says {e['pages']}, derived {derived}")
    n = sum(len(q["evidence"]) for q in load_questions())
    print(f"{bad} of {n} evidence patterns do not match" if bad else f"gold pages match ({n} evidence patterns)")
    return 1 if bad else 0


# --- what each system shows its user, as raw data (saved, so a run can be scored again) ---------

def aeids_raw(report: dict | None, status: str) -> dict[str, Any]:
    from aeids.reporting.answer import arrange

    findings = (report or {}).get("verified_findings") or []
    arranged = arrange(findings)
    claims = [{"text": r["value"], "calculated": True} for r in arranged["results"]]
    claims += [{"text": f"{r['value']} {r['units'] or ''}".strip(), "calculated": False} for r in arranged["reported"]]
    cites = sorted({(e["file"], e["page"]) for f in findings for e in f.get("evidence", [])})
    lines = [f"{r['label']}: {r['value']}" for r in arranged["results"]]
    lines += [f"{r['company'] or ''} {r['label']} {r['period'] or ''}: {r['value']} {r['units'] or ''}".strip()
              for r in arranged["reported"]]
    return {"found": bool(arranged["results"] or arranged["reported"]), "status": status,
            "summary": (report or {}).get("summary"), "text": "\n".join(lines), "claims": claims,
            "citations": [{"file": f, "pages": [p]} for f, p in cites],
            "limitations": (report or {}).get("limitations") or []}


def baseline_raw(out) -> dict[str, Any]:
    return {"found": out.found, "status": "error" if out.error else "done", "text": out.answer,
            "claims": [{"text": out.answer, "calculated": False}] if out.found else [],
            "citations": [{"file": c.filename, "pages": list(range(c.page_start, c.page_end + 1))}
                          for c in out.citations],
            "unknown_citations": out.unknown_citations,
            "sources": [f"{s.source_id} {s.filename} p.{s.page_start}-{s.page_end}" for s in out.sources]}


def to_answer(system: str, raw: dict[str, Any], question: str) -> SystemAnswer:
    ignore = [Decimal(n.replace(",", "")) for n in _NUM.findall(question)]
    claims = [c for x in raw["claims"] for c in mentions(x["text"], ignore=ignore, calculated=x["calculated"])]
    return SystemAnswer(system, raw["found"], raw.get("text", ""), claims,
                        [Citation(c["file"], tuple(c["pages"])) for c in raw["citations"]],
                        raw.get("latency_ms", 0.0), raw.get("tokens", 0),
                        Decimal(raw["cost_usd"]) if raw.get("cost_usd") is not None else None, raw.get("error"))


# --- running the systems ---------------------------------------------------------------------

def trace_usage(engine, trace_id: str | None) -> tuple[int, Decimal | None]:
    from sqlalchemy import func, select

    from aeids.db.models import TraceSpan

    if not trace_id:
        return 0, None
    with engine.connect() as conn:
        tokens, cost = conn.execute(select(func.sum(TraceSpan.total_tokens), func.sum(TraceSpan.cost_usd))
                                    .where(TraceSpan.trace_id == trace_id)).one()
    return int(tokens or 0), Decimal(str(cost)) if cost is not None else None


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        from aeids import tracing
        from aeids.agents.registry import build_registry, uses_llm
        from aeids.config import get_settings
        from aeids.db.session import make_engine, make_sessionmaker
        from aeids.llm.embedding import OpenRouterEmbedder
        from aeids.llm.openrouter import OpenRouterClient
        from aeids.retrieval.query_cache import SqlQueryCache
        from aeids.retrieval.vector_index import VectorIndex
        from aeids.storage import LocalStore

        self.settings = settings = get_settings()
        if not uses_llm(settings):
            raise SystemExit("AEIDS would run its stand-in agents (no OpenRouter key, or AEIDS_AGENT_MODE=stub); "
                             "the benchmark needs the LLM agents")
        self.engine = make_engine(settings.database_url)
        tracing.configure(tracing.SqlTraceSink(self.engine))  # tokens and cost are read from traces
        self.sessions = make_sessionmaker(self.engine)
        self.index = VectorIndex.from_url(settings.qdrant_url)
        self.embedder = OpenRouterEmbedder.from_settings(settings, cache=SqlQueryCache(self.engine))
        self.store = LocalStore(settings.storage_root)
        self.llm = OpenRouterClient(settings)
        self.registry = build_registry(settings)
        self.tenant_name = args.tenant

    def prepare(self) -> None:
        from aeids import documents as docs

        with self.sessions() as session:
            tenant = docs.get_or_create_tenant(session, self.tenant_name)
            session.commit()
            self.tenant_id = tenant.id
            prepare(session, self.store, self.index, self.embedder, tenant.id, load_corpus())

    def aeids(self, question: str) -> dict[str, Any]:
        from aeids.orchestration import runner
        from aeids.orchestration import supervisor as sup
        from aeids.db.models import Workflow

        t0 = time.perf_counter()
        with self.sessions() as session:
            wf = sup.create_workflow(session, self.tenant_id, question,
                                     {"company_ids": [], "document_ids": [], "fiscal_years": []},
                                     settings=self.settings, registry=self.registry)
            wf_id, trace_id = wf.id, wf.trace_id
        status = runner.drive(self.sessions, wf_id, registry=self.registry, settings=self.settings,
                              index=self.index, embedder=self.embedder)
        latency = (time.perf_counter() - t0) * 1000
        with self.sessions() as session:
            report = session.get(Workflow, wf_id).report
        raw = aeids_raw(report if status == "COMPLETED" else None, status)
        tokens, cost = trace_usage(self.engine, trace_id)
        raw.update(workflow_id=str(wf_id), trace_id=trace_id, latency_ms=round(latency, 1), tokens=tokens,
                   cost_usd=str(cost) if cost is not None else None,
                   error=None if status in ("COMPLETED", "NEEDS_HUMAN_REVIEW") else f"workflow ended {status}")
        return raw

    def baseline(self, question: str) -> dict[str, Any]:
        from aeids.baseline.rag import answer

        with self.sessions() as session:
            out = answer(session, self.index, self.embedder, self.llm, self.tenant_id, question)
        raw = baseline_raw(out)
        tokens, cost = trace_usage(self.engine, out.trace_id)
        cost = cost if cost is not None else out.cost_usd
        raw.update(trace_id=out.trace_id, latency_ms=out.latency_ms, tokens=tokens or out.total_tokens,
                   cost_usd=str(cost) if cost is not None else None,
                   error=out.error, model=out.model, prompt_version=out.prompt_version)
        return raw

    def close(self) -> None:
        self.registry.close()
        self.llm.close()
        self.embedder.client.close()
        self.index.close()
        self.engine.dispose()


# --- report -----------------------------------------------------------------------------------

def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v * 100:.0f}%"


def table(summaries: dict[str, dict[str, Any]], counts: dict[str, int]) -> str:
    systems = list(summaries)
    rows = [("Answer accuracy, all questions", lambda s: _pct(s["accuracy"]))]
    for t, label in (("lookup", "lookups"), ("calculation", "calculations"), ("comparison", "comparisons"),
                     ("not_found", "not in the reports (says so)")):
        if counts.get(t):
            rows.append((f"  {label} ({counts[t]})", lambda s, t=t: _pct(s["accuracy_by_type"].get(t))))
    rows += [
        ("Answerable questions left unanswered", lambda s: _pct(s["abstained_answerable"])),
        ("Citation precision (cited page is a gold page)", lambda s: _pct(s["citation_precision"])),
        ("Answers with a correct citation", lambda s: _pct(s["answers_with_correct_citation"])),
        ("Unsupported-claim rate (numbers stated)", lambda s: f"{_pct(s['unsupported_claim_rate'])} of {s['claims']}"),
        ("Latency, median / p95 (s)", lambda s: f"{s['latency_median_s']:.1f} / {s['latency_p95_s']:.1f}"),
        ("Tokens per question", lambda s: f"{s['tokens_per_question']:,.0f}"),
        ("Cost per question (USD)", lambda s: "–" if s["cost_per_question_usd"] is None else f"{s['cost_per_question_usd']:.4f}"),
        ("Stopped to ask a person (counts as no answer)", lambda s: str(s.get("needs_review", 0))),
        ("Runs with an error", lambda s: str(s["errors"])),
    ]
    head = "| Metric | " + " | ".join(NAMES[x] for x in systems) + " |"
    sep = "|---|" + "---:|" * len(systems)
    return "\n".join([head, sep] + [f"| {label} | " + " | ".join(f(summaries[x]) for x in systems) + " |"
                                    for label, f in rows])


def score_all(questions: list[dict], raws: dict[str, dict[str, dict]], pages: Pages,
              files: dict[str, str]) -> tuple[dict[str, list[QuestionScore]], dict[str, dict]]:
    scores: dict[str, list[QuestionScore]] = {}
    for system, by_q in raws.items():
        scores[system] = [score(to_question(q, files), to_answer(system, by_q[q["id"]], q["question"]), pages)
                          for q in questions if q["id"] in by_q]
    summaries = {s: summarise(v) for s, v in scores.items()}
    for s in summaries:  # AEIDS stops for a person when it cannot go on (e.g. a company has no report)
        summaries[s]["needs_review"] = sum(1 for r in raws[s].values() if r.get("status") == "NEEDS_HUMAN_REVIEW")
    return scores, summaries


def write(out: Path, meta: dict, questions: list[dict], raws: dict, scores: dict, summaries: dict) -> str:
    counts = {t: sum(1 for q in questions if q["type"] == t and all(q["id"] in raws[s] for s in raws))
              for t in ("lookup", "calculation", "comparison", "not_found")}
    md = table(summaries, counts)
    rows = []
    for q in questions:
        row = {"id": q["id"], "type": q["type"], "question": q["question"], "expected": q["expected"]}
        for s in raws:
            if q["id"] in raws[s]:
                sc = next(x for x in scores[s] if x.id == q["id"])
                row[s] = {**raws[s][q["id"]], "score": {
                    "correct": sc.correct, "matched": sc.matched, "citations": sc.citations,
                    "correct_citations": sc.correct_citations, "claims": sc.claims,
                    "unsupported": sc.unsupported_claims}}
        rows.append(row)
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({**meta, "summary": summaries, "questions": rows}, indent=2, default=str))
    out.with_suffix(".md").write_text(
        f"# AEIDS vs single-agent RAG baseline\n\n{meta['created_at']} · {len(rows)} questions · "
        f"corpus {', '.join(meta['corpus'])} · model {meta['llm_model']}\n\n{md}\n")
    return md


def run(args: argparse.Namespace) -> int:
    questions = [q for q in load_questions() if (not args.ids or q["id"] in args.ids)
                 and (not args.types or q["type"] in args.types)]
    if not questions:
        print("no questions selected", file=sys.stderr)
        return 1
    corpus = load_corpus()
    files = {r.key: r.file for r in corpus}
    pages = Pages()
    r = Runner(args)
    raws: dict[str, dict[str, dict]] = {s: {} for s in args.systems}
    try:
        r.prepare()
        print(f"\n{len(questions)} questions · systems: {', '.join(NAMES[s] for s in args.systems)} · "
              f"model {r.settings.llm_model}\n", flush=True)
        for q in questions:
            line = [f"{q['id']:<4} {q['type']:<11}"]
            for s in args.systems:
                try:
                    raw = getattr(r, s)(q["question"])
                except Exception as exc:  # a crash is a wrong answer, recorded, and the run goes on
                    raw = {"found": False, "status": "crash", "text": "", "claims": [], "citations": [],
                           "latency_ms": 0.0, "tokens": 0, "cost_usd": None, "error": f"{type(exc).__name__}: {exc}"[:500]}
                raws[s][q["id"]] = raw
                sc = score(to_question(q, files), to_answer(s, raw, q["question"]), pages)
                line.append(f"{NAMES[s].split()[0]} {'ok ' if sc.correct else 'NO '}{raw['latency_ms'] / 1000:5.1f}s")
            print("  ".join(line), flush=True)
    finally:
        r.close()

    scores, summaries = score_all(questions, raws, pages, files)
    stamp = datetime.now(UTC)
    meta = {"created_at": stamp.isoformat(), "tenant": args.tenant, "corpus": [c.key for c in corpus],
            "llm_model": r.settings.llm_model, "embedding_model": r.settings.embedding_model,
            "systems": args.systems}
    out = ROOT / "reports" / f"benchmark-{stamp:%Y%m%dT%H%M%SZ}.json"
    md = write(out, meta, questions, raws, scores, summaries)
    print(f"\n{md}\n\ndetails: {out.relative_to(ROOT)} (and .md)")
    return 0


def rescore(path: Path) -> int:
    data = json.loads(path.read_text())
    by_id = {q["id"]: q for q in load_questions()}
    questions = [by_id[row["id"]] for row in data["questions"] if row["id"] in by_id]
    raws = {s: {row["id"]: {k: v for k, v in row[s].items() if k != "score"} for row in data["questions"] if s in row}
            for s in data["systems"]}
    files = {r.key: r.file for r in load_corpus()}
    scores, summaries = score_all(questions, raws, Pages(), files)
    meta = {k: data[k] for k in ("created_at", "tenant", "corpus", "llm_model", "embedding_model", "systems")}
    out = path.with_name(path.stem + "-rescored.json")
    md = write(out, meta, questions, raws, scores, summaries)
    print(f"{md}\n\ndetails: {out.relative_to(ROOT)} (and .md)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eval/run_benchmark.py")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "check-gold", "rescore"])
    ap.add_argument("file", nargs="?", help="rescore: a saved reports/benchmark-*.json")
    ap.add_argument("--tenant", default="eval", help="tenant the corpus is loaded into")
    ap.add_argument("--systems", nargs="+", default=list(SYSTEMS), choices=SYSTEMS)
    ap.add_argument("--ids", nargs="+", help="only these question ids")
    ap.add_argument("--types", nargs="+", choices=["lookup", "calculation", "comparison", "not_found"])
    args = ap.parse_args(argv)
    if args.cmd == "check-gold":
        return check_gold()
    if args.cmd == "rescore":
        if not args.file:
            ap.error("rescore needs a saved benchmark file")
        return rescore(Path(args.file).resolve())
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
