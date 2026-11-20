"""app.recognise run twice on one result, the models stubbed out (no GPU, seconds):

    python test_recognise.py

  * an earlier entry is dropped when a new detection puts another box under its (page, id)
  * earlier chart results are kept when PaddleOCR-VL cannot start
  * the result file is replaced whole (no .tmp left behind)
"""

import json
import shutil

import app
import content

STEM = "_test_recognise"
TABLE = {"id": 1, "is_table": True, "bbox_pdf_pt": [10.2, 20.0, 300.0, 400.0]}
CHART = {"id": 1, "label": "chart", "text": "", "words": 0, "figure_class": "bar_chart", "figure_score": 0.9,
         "bbox_pdf_pt": [10.0, 450.0, 300.0, 700.0]}


class Doc:  # stands in for the PyMuPDF document: the stubs below never touch a page
    def __getitem__(self, i):
        return None

    def close(self):
        pass


def detect(table_box):
    path = app.tables_json(STEM)
    path.parent.mkdir(parents=True, exist_ok=True)
    pages = [{"page": 1, "detections": [{**TABLE, "bbox_pdf_pt": table_box}], "text_blocks": [CHART]},
             {"page": 2, "detections": [], "text_blocks": []}]
    path.write_text(json.dumps({"file": "x.pdf", "summary": {"total_pages": 2}, "pages": pages}), encoding="utf-8")


def recognise(pages=None):
    events = list(app.recognise(Doc(), STEM, "tables", pages))
    assert not [e for e in events if e["type"] == "error"], events
    return json.loads(app.structure_json(STEM).read_text(encoding="utf-8"))


content.structure_tatr.load = lambda *a: None
content.orienter = lambda: None
content.alive = lambda url: True
content.vlm_server = lambda: content.VLM_URL
content.render = lambda page, rect: None
content.chart_inputs = lambda page, block: ([None] * len(content.CHART_SCALES), "")
content.table = lambda page, det: {"page": 1, "table_id": det["id"], "bbox_pdf_pt": content.table_rect(det),
                                   "num_rows": 1, "num_cols": 1, "cells": [], "engine": "stub", "route": "stub",
                                   "orientation": 0, "complete": True}
content.read = lambda image, block: {"text": "a | 1", "words": 3, "read_by": "stub", "complete": True, "seconds": 0.0,
                                     "chart_table": content.chart_table("a | 1")}

try:
    detect(TABLE["bbox_pdf_pt"])
    first = recognise()
    assert [(s["page"], s["table_id"]) for s in first["tables"]] == [(1, 1)] and len(first["regions"]) == 1, first

    def no_vlm():
        raise RuntimeError("no llama-server")

    content.vlm_server = no_vlm  # B2: the chart is not read again, its earlier result stays
    again = recognise()
    assert len(again["tables"]) == 1 and [b["read_by"] for b in again["regions"]] == ["stub"], again

    detect([50.0, 20.0, 300.0, 400.0])  # B1: table #1 of page 1 is another box now; page 2 re-read only
    after = recognise(pages=[2])
    assert after["tables"] == [], after["tables"]
    assert not list(app.structure_json(STEM).parent.glob("*.tmp"))  # B3
    print("test_recognise ok")
finally:
    shutil.rmtree(app.OUT / STEM, ignore_errors=True)
