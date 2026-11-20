"""The retrieval benchmark files (eval/): well formed, and gold pages re-derivable."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("run_retrieval", ROOT / "eval" / "run_retrieval.py")
rr = importlib.util.module_from_spec(_spec)
sys.modules["run_retrieval"] = rr  # dataclasses look their module up while it loads
_spec.loader.exec_module(rr)


def test_corpus_and_questions_are_well_formed() -> None:
    corpus = {r.key: r for r in rr.load_corpus()}
    assert len(corpus) == 5 and all(re.fullmatch(r"[0-9a-f]{64}", r.sha256) for r in corpus.values())
    questions = rr.load_questions()
    assert len(questions) >= 20  # MVP_PLAN.md M2
    assert len({q["id"] for q in questions}) == len(questions)
    for q in questions:
        assert q["doc"] in corpus and q["pages"] and q["question"].endswith("?")
        assert all(1 <= p <= corpus[q["doc"]].pages for p in q["pages"])
        re.compile(q["evidence"])


def test_scoring() -> None:
    s = rr.score([1, 3, None, 12])
    assert s["recall@5"] == 0.5 and s["recall@10"] == 0.5
    assert s["mrr@10"] == pytest.approx((1 + 1 / 3) / 4)


@pytest.mark.skipif(any(rr.structure_path(r) is None for r in rr.load_corpus()),
                    reason="TableLens outputs of the corpus not on this machine")
def test_gold_pages_match_the_tablelens_output() -> None:
    assert rr.check_gold() == 0
