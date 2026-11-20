"""Check: scanned tables read with PaddleOCR-VL-1.6 "Spotting:" (each text line + its position) placed on Table
Transformer's grid, beside today's "Table Recognition:" result. The pipeline is not changed; everything goes to
output/<name>/spotting_check/ (run.log, one JSON + one PNG per table).

On the Modal GPU server (logs stream to this terminal, results come back to output/ here):

    python -m modal run gpu_server.py::check_spotting --pdf <pdf> --tables 3:1,4:2

On this computer:

    python spotting_check.py prepare       # once: a copy of PaddleOCR-VL's mmproj with the image limit Spotting needs
    <llama-server command prepare prints>   # in a second window: PaddleOCR-VL on port 8081 with that copy
    python spotting_check.py run <pdf> --tables 3:1,4:2 [--url http://127.0.0.1:8081] [--coords norm1000|pixels]

Why: "Table Recognition:" builds its grid from the printed column headings. A table with more value columns than
headings (items, subtotals and totals printed further right under one year) loses values: a row's second number
slides into the next heading's column and pushes that column's own value out. Here the text comes from PaddleOCR-VL
with positions, and the columns from Table Transformer, which sees where the values are (as for text-layer tables).

Per table (page:id of the detection result): the table rendered as content.table renders a scan (216 dpi, upright
tables only); PaddleOCR-VL "Spotting:" on it; Table Transformer's grid (structure_tatr.steadiest with the models
content.table uses); each spotted line put in the cell it overlaps most (structure_tatr.fill). Logged: the raw answer,
the lines, both grids, numbers each grid lost against the spotted ones, cells holding two numbers (a line across two
columns). PNG: spotted boxes red, grid cells blue.
"""

import argparse
import json
import logging
import re
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

import pymupdf
from PIL import ImageDraw

LOG = logging.getLogger("spotting_check")
MODELS = Path(__file__).with_name("models")
SPOT_MMPROJ = MODELS / "PaddleOCR-VL-1.6-spotting-mmproj.gguf"
SPOT_PIXELS = 1605632  # clip.vision.image_max_pixels the model card asks for Spotting (default 1003520)
LOC = re.compile(r"(?:<\|LOC_(\d+)\|>\s*){4,}")
NUM = re.compile(r"-?\d[\d.,]*\d|\d")


def setup_log(path=None):
    LOG.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    for h in [logging.StreamHandler(sys.stdout)] + ([logging.FileHandler(path, "w", encoding="utf-8")] if path else []):
        h.setFormatter(fmt)
        LOG.addHandler(h)


# ---------- prepare: the mmproj copy ----------

def prepare():
    """Copy PaddleOCR-VL-1.6's mmproj to models/ and raise its clip.vision.image_max_pixels to SPOT_PIXELS (the copy
    only: the pipeline's llama-server keeps the original). Returns the llama-server command for the check (port 8081,
    -sp: the model's special tokens kept in its answer, as Spotting's positions are <|LOC_n|> tokens)."""
    from gguf import GGUFReader
    from huggingface_hub import hf_hub_download

    VLM_REPO = "PaddlePaddle/PaddleOCR-VL-1.6-GGUF"  # content.VLM_REPO (content is not imported: it loads torch)
    model = hf_hub_download(VLM_REPO, "PaddleOCR-VL-1.6-GGUF.gguf")
    src = hf_hub_download(VLM_REPO, "PaddleOCR-VL-1.6-GGUF-mmproj.gguf")
    LOG.info("mmproj (original, unchanged): %s", src)
    MODELS.mkdir(exist_ok=True)
    shutil.copyfile(src, SPOT_MMPROJ)
    LOG.info("copied to %s", SPOT_MMPROJ)
    reader = GGUFReader(SPOT_MMPROJ, "r+")
    field = reader.get_field("clip.vision.image_max_pixels")
    if field is None:
        raise SystemExit("this mmproj has no clip.vision.image_max_pixels field: llama.cpp / model version differs")
    before = int(field.parts[field.data[0]][0])
    field.parts[field.data[0]][0] = SPOT_PIXELS
    reader.data.flush()
    after = int(GGUFReader(SPOT_MMPROJ).get_field("clip.vision.image_max_pixels").parts[field.data[0]][0])
    LOG.info("clip.vision.image_max_pixels %d -> %d", before, after)
    if after != SPOT_PIXELS:
        raise SystemExit("the value did not change")
    return [shutil.which("llama-server") or "llama-server", "-m", model, "--mmproj", str(SPOT_MMPROJ),
            "--host", "127.0.0.1", "--port", "8081", "--temp", "0", "-ngl", "99", "-c", "16384", "-sp"]


def serve(log_path):
    """prepare(), then that llama-server started here (its output to log_path); returns the process once it answers.
    For the GPU server (gpu_server.check_spotting), where nobody opens a second window."""
    import subprocess
    import urllib.request

    cmd = prepare()
    LOG.info("starting the check's llama-server: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=open(log_path, "ab"), stderr=subprocess.STDOUT)
    t = time.time()
    while time.time() - t < 300:
        if proc.poll() is not None:
            raise SystemExit(f"llama-server exited ({proc.returncode}): see {log_path}")
        try:
            with urllib.request.urlopen("http://127.0.0.1:8081/health", timeout=3) as r:
                if r.status == 200:
                    LOG.info("llama-server answers on 8081 after %.0f s", time.time() - t)
                    return proc
        except OSError:
            pass
        time.sleep(1)
    proc.kill()
    raise SystemExit(f"llama-server did not answer within 5 minutes: see {log_path}")


# ---------- spotting answer -> lines ----------

def parse_spotting(text, w, h, coords="norm1000"):
    """PaddleOCR-VL "Spotting:" answer -> [(x0, y0, x1, y1, text)] in image pixels (w x h). Each line's position is
    a run of <|LOC_n|> tokens (8: four corners, or 4: two); the run comes after its text, or before it when the
    answer starts with one. coords: norm1000 = 0..1000 of the image, pixels = image pixels."""
    runs = list(LOC.finditer(text))
    if not runs:
        return []
    first = not text[:runs[0].start()].strip()  # a location opens the answer: each one precedes its text
    out = []
    for i, m in enumerate(runs):
        if first:
            s = text[m.end():runs[i + 1].start() if i + 1 < len(runs) else len(text)]
        else:
            s = text[runs[i - 1].end() if i else 0:m.start()]
        s = " ".join(s.split())
        n = [int(x) for x in re.findall(r"<\|LOC_(\d+)\|>", m.group(0))]
        xs, ys = (n[0:8:2], n[1:8:2]) if len(n) >= 8 else ([n[0], n[2]], [n[1], n[3]])
        fx, fy, d = (w, h, 1000) if coords == "norm1000" else (1, 1, 1)
        if s:
            out.append((min(xs) * fx / d, min(ys) * fy / d, max(xs) * fx / d, max(ys) * fy / d, s))
    return out


# ---------- grids ----------

def grid_lines(cells, nrows, ncols):
    at = {(c["row"], c["col"]): (c.get("text") or "") for c in cells}
    return [" | ".join((at.get((r, q), "·")[:30].ljust(30) if q == 0 else at.get((r, q), "·")[:14].rjust(14))
                       for q in range(ncols)) for r in range(nrows)]


def numbers(texts):
    return Counter(n for s in texts for n in NUM.findall(s or "") if any(ch in n for ch in ".,") or len(n) > 2)


def check_table(page, det, old, scale, coords, out_dir):
    """One table: spotting + Table Transformer grid, logged and saved. Returns a summary dict."""
    import content
    import structure_tatr

    key = f"p{page.number + 1}_t{det['id']}"
    rect = content.table_rect(det)
    LOG.info("[%s] table box %s pt", key, rect)
    image = content.render(page, rect, scale)
    LOG.info("[%s] rendered %dx%d px at %d dpi", key, image.width, image.height, round(72 * scale))
    turn = content.angles([image])[0]
    if turn:
        LOG.warning("[%s] the orientation model says the table is turned %d°: this check handles upright tables only, "
                    "skipped", key, turn)
        return {"table": key, "skipped": f"turned {turn}"}

    t = time.time()
    LOG.info("[%s] PaddleOCR-VL Spotting: … (CPU can take minutes)", key)
    answer, complete = content.vlm(image, "Spotting:")
    LOG.info("[%s] answer: %d characters in %.1f s%s", key, len(answer), time.time() - t,
             "" if complete else " · CUT OFF at max_tokens")
    LOG.info("[%s] answer starts: %r", key, answer[:300])
    lines = parse_spotting(answer, image.width, image.height, coords)
    if not lines:
        LOG.error("[%s] no <|LOC_n|> positions in the answer. Plain text means llama-server dropped the model's "
                  "special tokens: start it with -sp (the command `prepare` prints).", key)
        (out_dir / f"{key}.json").write_text(json.dumps({"answer": answer}, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"table": key, "error": "no positions"}
    xs, ys = [v for l in lines for v in (l[0], l[2])], [v for l in lines for v in (l[1], l[3])]
    LOG.info("[%s] %d lines with positions · x %.0f..%.0f of %d px · y %.0f..%.0f of %d px", key, len(lines),
             min(xs), max(xs), image.width, min(ys), max(ys), image.height)
    if max(xs) > image.width * 1.05 or max(ys) > image.height * 1.05:
        LOG.warning("[%s] positions run past the image: try --coords %s", key,
                    "pixels" if coords == "norm1000" else "norm1000")
    # image pixels -> PDF points (the render's clip starts at the table box, cut at the page edge)
    ox, oy = max(rect[0], page.rect.x0), max(rect[1], page.rect.y0)
    words = [(ox + x0 / scale, oy + y0 / scale, ox + x1 / scale, oy + y1 / scale, s, 0, i, 0)
             for i, (x0, y0, x1, y1, s) in enumerate(lines)]

    t = time.time()
    models = [structure_tatr.load(content.STRUCTURE_MODEL), structure_tatr.load(structure_tatr.MODEL)]
    objs, model, agree = structure_tatr.steadiest(models, page, rect, 0)
    cells, nrows, ncols = structure_tatr.build_cells(objs)
    structure_tatr.fill(cells, words)
    LOG.info("[%s] Table Transformer (%s): %d×%d grid, views agree %.3f, %.1f s", key, Path(model.name_or_path).name,
             nrows, ncols, agree, time.time() - t)

    spotted = numbers(l[4] for l in lines)
    new_lost = spotted - numbers(c["text"] for c in cells)
    merged = [(c["row"], c["col"], c["text"]) for c in cells if len(numbers([c["text"]])) > 1]
    LOG.info("[%s] numbers spotted: %d", key, sum(spotted.values()))
    if old:
        old_nums = numbers(c.get("text") for c in old["cells"])
        LOG.info("[%s] TODAY (Table Recognition) %d×%d · numbers lost vs spotted: %d %s · not spotted: %d %s", key,
                 old["num_rows"], old["num_cols"], sum((spotted - old_nums).values()), sorted(spotted - old_nums),
                 sum((old_nums - spotted).values()), sorted(old_nums - spotted))
        for s in grid_lines(old["cells"], old["num_rows"], old["num_cols"]):
            LOG.info("[%s]   today | %s", key, s)
    else:
        LOG.info("[%s] no earlier result for this table in the _structure.json", key)
    LOG.info("[%s] NEW (Spotting + Table Transformer) %d×%d · numbers lost vs spotted: %d %s", key, nrows, ncols,
             sum(new_lost.values()), sorted(new_lost))
    for s in grid_lines(cells, nrows, ncols):
        LOG.info("[%s]   new   | %s", key, s)
    for r, q, s in merged:
        LOG.warning("[%s] cell r%d c%d holds two or more numbers (a spotted line across columns?): %r", key, r, q, s)

    draw = ImageDraw.Draw(image)
    for c in cells:
        b = c["bbox_pdf_pt"]
        draw.rectangle([(b[0] - ox) * scale, (b[1] - oy) * scale, (b[2] - ox) * scale, (b[3] - oy) * scale],
                       outline=(40, 90, 230), width=1)
    for x0, y0, x1, y1, _ in lines:
        draw.rectangle([x0, y0, x1, y1], outline=(230, 30, 30), width=2)
    image.save(out_dir / f"{key}.png")
    (out_dir / f"{key}.json").write_text(json.dumps(
        {"table": key, "box_pdf_pt": rect, "dpi": round(72 * scale), "complete": complete, "answer": answer,
         "lines_px": lines, "grid": {"num_rows": nrows, "num_cols": ncols, "model": Path(model.name_or_path).name,
                                     "agreement": agree, "cells": cells},
         "today": old and {k: old[k] for k in ("num_rows", "num_cols", "cells")}}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    LOG.info("[%s] saved %s.png (red: spotted lines, blue: grid) and %s.json", key, key, key)
    return {"table": key, "spotted_numbers": sum(spotted.values()), "new_lost": sum(new_lost.values()),
            "today_lost": sum((spotted - numbers(c.get("text") for c in old["cells"])).values()) if old else None,
            "merged_cells": len(merged), "grid": f"{nrows}x{ncols}", "today": old and f"{old['num_rows']}x{old['num_cols']}"}


def run(pdf, tables, url, coords):
    from app import safe_stem  # the web app's module: loads the model stack
    import content
    import structure

    stem = safe_stem(Path(pdf).name)
    out_dir = structure.OUT / stem / "spotting_check"
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_log(out_dir / "run.log")
    LOG.info("PDF %s · tables %s · PaddleOCR-VL at %s · coords %s", pdf, tables, url, coords)
    # vlm() asks only this server: if it goes down, the run fails rather than starting the pipeline's own llama-server
    # (that one lacks the spotting image limit)
    content.VLM_URL = url
    content.vlm_server = lambda: url
    if not content.alive(url):
        LOG.error("nothing answers at %s: start llama-server with the spotting mmproj first (python spotting_check.py "
                  "prepare prints the command). Not starting one here: the pipeline's own would lack the image limit.", url)
        raise SystemExit(1)
    LOG.info("PaddleOCR-VL answers at %s", url)
    detected = json.loads(structure.tables_json(stem).read_text(encoding="utf-8"))
    path = structure.structure_json(stem)
    old = {(t["page"], t["table_id"]): t for t in json.loads(path.read_text(encoding="utf-8"))["tables"]} \
        if path.exists() else {}
    LOG.info("detection %s · earlier recognition %s", structure.tables_json(stem).name,
             path.name if path.exists() else "none")
    dets = {(p["page"], d["id"]): d for p in detected["pages"] for d in p["detections"] if d["is_table"]}
    results, t0 = [], time.time()
    with pymupdf.open(pdf) as doc:
        for page_no, tid in tables:
            det = dets.get((page_no, tid))
            if det is None:
                LOG.error("[p%d_t%d] no detected table with that page and id in %s", page_no, tid,
                          structure.tables_json(stem).name)
                continue
            t = time.time()
            try:
                results.append(check_table(doc[page_no - 1], det, old.get((page_no, tid)), content.TABLE_SCALE,
                                           coords, out_dir))
            except Exception:
                LOG.exception("[p%d_t%d] failed", page_no, tid)
                results.append({"table": f"p{page_no}_t{tid}", "error": "failed (see above)"})
            LOG.info("[p%d_t%d] done in %.1f s", page_no, tid, time.time() - t)
    LOG.info("SUMMARY (%.1f s) · numbers lost against the spotted ones: today vs new", time.time() - t0)
    for r in results:
        LOG.info("  %s", " · ".join(f"{k} {v}" for k, v in r.items()))
    (out_dir / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    LOG.info("log: %s", out_dir / "run.log")
    return out_dir


def parse_tables(s):
    """'21:1,22:2' -> [(21, 1), (22, 2)] (page, detection id)."""
    return [tuple(int(x) for x in part.split(":")) for part in s.split(",") if part.strip()]


def selfcheck():
    """Positions after or before their text, corners or two points, 0..1000 of the image."""
    loc = lambda *n: "".join(f"<|LOC_{v}|>" for v in n)
    after = f"Sales{loc(0, 0, 500, 0, 500, 100, 0, 100)}\n1.234,5{loc(600, 0, 900, 100)}"
    assert parse_spotting(after, 200, 100) == [(0, 0, 100, 10, "Sales"), (120, 0, 180, 10, "1.234,5")], parse_spotting(after, 200, 100)
    before = f"{loc(0, 0, 500, 100)}Sales\n{loc(600, 0, 900, 100)} 12"
    assert [l[4] for l in parse_spotting(before, 200, 100)] == ["Sales", "12"]
    assert parse_spotting("plain text, no positions", 10, 10) == []
    assert numbers(["A 1.234,56 and 12", "7", "-3,5 2020"]) == Counter({"1.234,56": 1, "-3,5": 1, "2020": 1})
    assert parse_tables("3:1, 4:2") == [(3, 1), (4, 2)]


if __name__ == "__main__":
    selfcheck()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare", help="mmproj copy with the image limit Spotting needs; prints the llama-server command")
    r = sub.add_parser("run")
    r.add_argument("pdf")
    r.add_argument("--tables", type=parse_tables, required=True, help="page:id of detected tables, e.g. 3:1,4:2")
    r.add_argument("--url", default="http://127.0.0.1:8081", help="llama-server running the spotting mmproj")
    r.add_argument("--coords", choices=("norm1000", "pixels"), default="norm1000",
                   help="how the model's <|LOC_n|> positions are scaled (the log warns when they look wrong)")
    a = ap.parse_args()
    if a.cmd == "prepare":
        setup_log()
        cmd = prepare()
        LOG.info("now start PaddleOCR-VL for the check in a second window (leave it running):")
        print("\n" + " ".join(f'"{c}"' if " " in c or "\\" in c else c for c in cmd) + "\n")
    else:
        run(a.pdf, a.tables, a.url, a.coords)
