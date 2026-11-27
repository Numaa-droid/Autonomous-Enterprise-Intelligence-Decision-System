"""Retrieval evaluation on the 5-company corpus (MVP_PLAN.md M2, D-008).

    .venv/bin/python eval/run_retrieval.py              # load + index the corpus if needed, then score
    .venv/bin/python eval/run_retrieval.py check-gold   # only re-derive the gold pages (no services)
    .venv/bin/python eval/run_retrieval.py --reindex    # embed the corpus again first (D-023)

What it does:
1. Loads every report in ``eval/corpus.csv`` into the ``eval`` tenant (kept apart from your
   own uploads): the PDF is stored and checked against its SHA-256, the existing TableLens
   ``_structure.json`` is attached (no GPU needed), and the version is indexed with the
   configured embedding model (OpenRouter; about 2,000 chunks, well under one US cent). Steps
   already done are skipped, so a re-run only scores.
2. Asks each question in ``eval/retrieval_questions.jsonl`` in three modes (dense, keyword,
   hybrid) over all five reports, without company filters.
3. Reports per mode: Recall@5 and Recall@10 (the share of questions with at least one gold page
   of the right report in the top K) and MRR@10 (mean of 1/rank of the first such hit).
   A hit is a chunk of the question's report whose page range includes a gold page.

Repeatability (D-023): embeddings come from one pinned OpenRouter provider and question vectors
are cached in Postgres, so a second run with the same index gives the same ranks; the report
records the provider and how many question vectors came from the cache.

Gold pages: each question has an ``evidence`` pattern (the printed answer, e.g. the value in
the statement); its gold pages are the pages of the report whose text matches it. They are
stored in the file, and ``check-gold`` re-derives them from the TableLens output, so anyone can
check them. Results go to ``reports/retrieval-<time>.json`` (git-ignored).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EVAL = ROOT / "eval"
CORPUS = EVAL / "corpus.csv"
QUESTIONS = EVAL / "retrieval_questions.jsonl"
MODES = ("dense", "keyword", "hybrid")
KS = (5, 10)
# where the PDFs and TableLens outputs are: this repo's tablelens/ first, else the old folder
SEARCH_DIRS = [ROOT / "tablelens", ROOT.parent / "IntelligencePDF"]


@dataclass(frozen=True)
class Report:
    key: str
    company: str
    document_type: str
    fiscal_year: int
    file: str
    sha256: str
    pages: int
    source_url: str

    @property
    def stem(self) -> str:
        return Path(self.file).stem


def load_corpus() -> list[Report]:
    with CORPUS.open(newline="") as fh:
        return [Report(r["key"], r["company"], r["document_type"], int(r["fiscal_year"]), r["file"],
                       r["sha256"], int(r["pages"]), r.get("source_url", "")) for r in csv.DictReader(fh)]


def load_questions() -> list[dict]:
    return [json.loads(line) for line in QUESTIONS.read_text().splitlines() if line.strip()]


def find(relative: str) -> Path | None:
    return next((d / relative for d in SEARCH_DIRS if (d / relative).is_file()), None)


def structure_path(report: Report) -> Path | None:
    return find(f"output/{report.stem}/structure/{report.stem}_structure.json")


def page_texts(structure: dict) -> dict[int, str]:
    """Text per page from a TableLens structure: regions, table cells and captions."""
    parts: dict[int, list[str]] = {}
    for r in structure.get("regions", []):
        parts.setdefault(r["page"], []).append(r.get("text") or "")
    for t in structure.get("tables", []):
        parts.setdefault(t["page"], []).append(" ".join(c.get("text") or "" for c in t.get("cells", [])))
        parts[t["page"]] += [t[k] for k in ("title", "caption", "subtitle") if t.get(k)]
    return {p: " ".join(" ".join(v).split()) for p, v in parts.items()}


def gold_pages(texts: dict[int, str], evidence: str) -> list[int]:
    rx = re.compile(evidence, re.IGNORECASE)
    return sorted(p for p, t in texts.items() if rx.search(t))


# --- check-gold -----------------------------------------------------------------

def check_gold() -> int:
    corpus = {r.key: r for r in load_corpus()}
    texts: dict[str, dict[int, str]] = {}
    bad = 0
    for q in load_questions():
        report = corpus[q["doc"]]
        if report.key not in texts:
            path = structure_path(report)
            if path is None:
                print(f"missing TableLens output for {report.key}", file=sys.stderr)
                return 1
            texts[report.key] = page_texts(json.loads(path.read_text()))
        derived = gold_pages(texts[report.key], q["evidence"])
        ok = derived == q["pages"] and derived
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {q['id']:<4} pages {q['pages']} derived {derived}")
    print(f"{bad} question(s) with gold pages that do not match" if bad else "gold pages match")
    return 1 if bad else 0


# --- load + index ------------------------------------------------------------------

def prepare(session, store, index, embedder, tenant_id, corpus: list[Report],
            reindex: bool = False) -> dict[str, object]:
    """Document per report key, loaded and indexed (steps already done are skipped; with
    ``reindex``, indexed versions are embedded again in place)."""
    from aeids import documents as docs
    from aeids.db.models import DocumentType, ParserStatus
    from aeids.ingestion.indexing import index_version, reindex_version
    from aeids.ingestion.pipeline import ingest_structure

    out = {}
    for r in corpus:
        pdf = find(f"doc/{r.file}")
        structure = structure_path(r)
        if pdf is None or structure is None:
            raise SystemExit(f"{r.key}: PDF or TableLens output not found (looked in "
                             f"{', '.join(str(d) for d in SEARCH_DIRS)})")
        sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        if sha != r.sha256:
            raise SystemExit(f"{r.key}: {pdf.name} has SHA-256 {sha[:12]}…, corpus.csv says "
                             f"{r.sha256[:12]}… (a different file)")
        with pdf.open("rb") as fh:
            staged = docs.stage_upload(fh, r.file, store.staging_dir(), 500 * 1024 * 1024)
        outcome = docs.create_document(session, store, tenant_id, staged, company_name=r.company,
                                       document_type=DocumentType(r.document_type),
                                       fiscal_year=r.fiscal_year)
        version = outcome.version
        status = version.parser_status
        if status in (ParserStatus.PENDING.value, ParserStatus.FAILED.value) and version.parsed_at is None:
            print(f"{r.key}: attaching TableLens output …", flush=True)
            ingest_structure(session, store, tenant_id, version.document_id, version.id,
                             structure.read_bytes())
            status = ParserStatus.PARSED.value
        if status != ParserStatus.INDEXED.value or reindex:
            t0 = time.monotonic()
            again = status == ParserStatus.INDEXED.value
            print(f"{r.key}: {'re-' if again else ''}embedding and indexing …", flush=True)
            res = (reindex_version if again else index_version)(session, index, embedder, tenant_id,
                                                                version.id)
            print(f"{r.key}: {res.point_count} chunks indexed in {time.monotonic() - t0:.0f} s",
                  flush=True)
        out[r.key] = outcome.document
    return out


# --- scoring ----------------------------------------------------------------------

def first_hit(hits, document_id, pages: list[int]) -> int | None:
    gold = set(pages)
    for rank, h in enumerate(hits, start=1):
        if h.document_id == document_id and gold & set(range(h.page_start, h.page_end + 1)):
            return rank
    return None


def score(ranks: list[int | None]) -> dict[str, float]:
    n = len(ranks) or 1
    out = {f"recall@{k}": sum(1 for r in ranks if r is not None and r <= k) / n for k in KS}
    out["mrr@10"] = sum(1 / r for r in ranks if r is not None and r <= 10) / n
    return out


def ask_all(session, index, embedder, tenant_id, documents, questions, modes):
    """Every question in every mode -> (ranks per mode, per-question rows)."""
    from aeids.retrieval.search import search

    ranks: dict[str, list[int | None]] = {m: [] for m in modes}
    rows = []
    for q in questions:
        row = {"id": q["id"], "doc": q["doc"], "question": q["question"], "pages": q["pages"]}
        for mode in modes:
            result = search(session, index, embedder, tenant_id, q["question"], mode=mode,
                            top_k=max(KS))
            rank = first_hit(result.hits, documents[q["doc"]].id, q["pages"])
            ranks[mode].append(rank)
            row[mode] = {"rank": rank, "top": [
                {"doc": h.filename, "pages": [h.page_start, h.page_end], "section": h.section,
                 "kind": h.kind} for h in result.hits[:3]], "warnings": result.warnings}
        rows.append(row)
    return ranks, rows


def run(args: argparse.Namespace) -> int:
    from aeids import documents as docs
    from aeids import tracing
    from aeids.config import get_settings
    from aeids.db.session import make_engine, make_sessionmaker
    from aeids.llm.embedding import OpenRouterEmbedder
    from aeids.retrieval.query_cache import SqlQueryCache
    from aeids.retrieval.vector_index import VectorIndex
    from aeids.storage import LocalStore

    settings = get_settings()
    corpus = load_corpus()
    questions = load_questions()
    engine = make_engine(settings.database_url)
    if settings.tracing_enabled:
        tracing.configure(tracing.SqlTraceSink(engine))
    index = VectorIndex.from_settings(settings)
    embedder = OpenRouterEmbedder.from_settings(settings, cache=SqlQueryCache(engine))
    store = LocalStore(settings.storage_root)
    try:
        with make_sessionmaker(engine)() as session:
            tenant = docs.get_or_create_tenant(session, args.tenant)
            session.commit()
            documents = prepare(session, store, index, embedder, tenant.id, corpus,
                                reindex=args.reindex)

            with tracing.span("retrieval_eval", "eval", tenant_id=tenant.id,
                              inputs={"questions": len(questions), "modes": args.modes,
                                      "reports": [r.key for r in corpus]}) as eval_span:
                ranks, rows = ask_all(session, index, embedder, tenant.id, documents, questions,
                                      args.modes)
                eval_span.set_output({m: score(ranks[m]) for m in args.modes})
    finally:
        embedder.client.close()
        index.close()
        engine.dispose()

    scores = {m: score(ranks[m]) for m in args.modes}
    provider = settings.embedding_provider or "any provider"
    asked = len(questions) * sum(m != "keyword" for m in args.modes)
    print(f"\nRetrieval on {len(corpus)} reports, {len(questions)} questions "
          f"(embeddings {settings.embedding_model} via {provider}; "
          f"{embedder.cache_hits}/{asked} question vectors from the cache)\n")
    print(f"{'mode':<9} {'Recall@5':>9} {'Recall@10':>10} {'MRR@10':>8}")
    for m in args.modes:
        s = scores[m]
        print(f"{m:<9} {s['recall@5']:>9.2f} {s['recall@10']:>10.2f} {s['mrr@10']:>8.2f}")
    missed = [r["id"] for r in rows if all(r[m]["rank"] is None for m in args.modes)]
    if missed:
        print(f"\nnot found in the top {max(KS)} by any mode: {', '.join(missed)}")
    print(f"\nembedding usage: {embedder.usage.total_tokens} tokens, "
          f"cost {embedder.usage.cost if embedder.usage.cost is not None else 'n/a'} USD")

    out = ROOT / "reports" / f"retrieval-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "created_at": datetime.now(UTC).isoformat(), "embedding_model": settings.embedding_model,
        "embedding_provider": settings.embedding_provider, "query_cache_hits": embedder.cache_hits,
        "tenant": args.tenant, "corpus": [r.key for r in corpus], "scores": scores,
        "questions": rows}, indent=2))
    print(f"details: {out.relative_to(ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="eval/run_retrieval.py")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "check-gold"])
    ap.add_argument("--tenant", default="eval", help="tenant the corpus is loaded into")
    ap.add_argument("--modes", nargs="+", default=list(MODES), choices=MODES)
    ap.add_argument("--reindex", action="store_true",
                    help="embed the corpus again in place first (after changing provider or model)")
    args = ap.parse_args(argv)
    return check_gold() if args.cmd == "check-gold" else run(args)


if __name__ == "__main__":
    sys.exit(main())
