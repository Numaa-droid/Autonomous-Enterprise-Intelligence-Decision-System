"""Table Transformer structure (microsoft/table-transformer-structure-recognition-v1.1-all, MIT) on the tables
app.py found — a second structure model beside TableFormerV2 (structure.py), to compare on hard tables.

    python structure_tatr.py <pdf> --pages 10-12

The model draws boxes instead of generating cells token by token: full-width rows, full-height columns,
the column-header area, projected row headers (section rows) and spanning cells. The grid is rows x columns,
a spanning cell merges the positions it covers, and fill() puts each PDF word in the cell it overlaps most.
Writes output/<name>/structure/<name>_structure_tatr.json in the _structure.json format, so the UI opens it the same way.
"""

import argparse
import functools
import json
import math
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pymupdf
import torch
from huggingface_hub import hf_hub_download
from PIL import Image
from transformers import TableTransformerConfig, TableTransformerForObjectDetection

from structure import MEAN, OUT, STD, parse_pages, structure_json, tables_json

MODEL = "microsoft/table-transformer-structure-recognition-v1.1-all"
THRESHOLD = 0.5   # every structure class, as the official inference.py
# test-time augmentation: (PDF points around the detected table, longest image side in px) per model run. The
# first is the official setting (MaxResize(1000)); rows and columns come from it. Spanning cells, the column
# header and section rows change with the crop when the model is unsure (many spanning cells in one run, a few
# in another), so they are fused over all runs and kept only where most runs agree.
VIEWS = [(5, 1000), (10, 800), (10, 1300), (15, 1000)]
FUSED = {"table spanning cell", "table column header", "table projected row header"}
THIN = 1.5  # points: a filled rectangle at most this thick is a ruling line (ruled())
GAP = 2.5   # points: ruling lines closer than this are one line (a double border)
# steadiest(): how much more a later model's views must agree to replace an earlier one. A model unsure of a table
# agrees far less (rows 39 / 13 / 40 / 24 across views); a 0.01 lead is noise, not a better grid.
# ponytail: one fixed margin, tune it on the Azure-labelled test set if fallbacks look wrong
MARGIN = 0.05
# text_rows(): the PDF text layer checks the model's rows
ROW_TOL = 3   # points: line tops closer than this are level (one printed line, or one printed row across columns)
SMALL = 0.88  # a line whose words are less than this times the table's median word height is small print
KEY = 0.8     # a column filled in at least this share of the body rows is a key column (every printed row fills it)


@functools.cache
def load(name=MODEL):
    # v1.1's config.json has dilation / backbone = null, which transformers 5 rejects: drop them (defaults apply).
    # name is a Hugging Face model id or a local folder (a fine-tuned model: models/tatr-finetuned-01-11)
    path = Path(name) / "config.json" if Path(name).is_dir() else Path(hf_hub_download(name, "config.json"))
    config = json.loads(path.read_text())
    config = TableTransformerConfig(**{k: v for k, v in config.items() if v is not None})
    return TableTransformerForObjectDetection.from_pretrained(name, config=config).eval()


def objects(model, page, rect, turn=0):
    """Model boxes for the table at rect (PDF points), in PDF points (upright frame, see crop()): rows and columns
    of the first of VIEWS, the FUSED classes fused over all of them."""
    return merge([view(model, page, rect, pad, side, turn) for pad, side in VIEWS])


def merge(runs):
    return [o for o in runs[0] if o[0] not in FUSED] + fuse([[o for o in r if o[0] in FUSED] for r in runs])


def agreement(runs):
    """How much the VIEWS agree on the grid: mean F1 of each later view's rows and columns matched one-to-one
    (1-D IoU >= 0.5) to the first view's. 1 = every view drew the same grid; a model unsure of a table draws
    a different one per crop (e.g. 39, 13, 40 and 24 rows for the same table)."""
    iou = lambda a, b: max(0, min(a[1], b[1]) - max(a[0], b[0])) / (max(a[1], b[1]) - min(a[0], b[0]))
    out = []
    for name, lo, hi in (("table row", 1, 3), ("table column", 0, 2)):
        ref, *rest = [dedupe([(s, b) for l, s, b in r if l == name], lo, hi) for r in runs]
        for bands in rest:
            free, hit = list(bands), 0
            for a in ref:
                b = max(free, key=lambda b: iou(a, b), default=None)
                if b is not None and iou(a, b) >= 0.5:
                    free.remove(b)
                    hit += 1
            out.append(2 * hit / (len(ref) + len(bands)) if ref or bands else 1.0)
    return sum(out) / len(out)


def steadiest(models, page, rect, turn=0):
    """(objects, model, agreement) of the model whose VIEWS agree most on the table's grid. models: best first; a
    later model takes over only when its views agree more than MARGIN better -- views that agree are not views that
    are right, so a near-tie keeps the better model. A later model only runs while it could still win. turn: see
    crop()."""
    best = None
    for model in models:
        runs = [view(model, page, rect, pad, side, turn) for pad, side in VIEWS]
        a = agreement(runs)
        if best is None or a > best[0] + MARGIN:
            best = (a, runs, model)
        if best[0] >= 1 - MARGIN:
            break
    return merge(best[1]), best[2], best[0]


def crop(page, rect, pad, turn=0):
    """(image, [x0, y0, x1, y1] it shows in PDF points): the table with pad points around it, at 144 dpi, cut at
    the page edge. turn (degrees, upright()) renders a sideways table upright; the box is then in the upright
    frame (PDF points turned by pymupdf.Matrix(turn), see frame()). rect is in the unrotated frame (as the PDF
    words and drawings); a page stored rotated (/Rotate) renders already turned by page.rotation and takes its
    clip in that displayed frame, so the clip is converted and only the rest of the turn is added."""
    clip = pymupdf.Rect(rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad) & (page.rect * page.derotation_matrix)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2).prerotate(turn - page.rotation), clip=clip * page.rotation_matrix)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), frame(clip, turn)


def upright(page, rect):
    """Degrees (0 / 90 / 180 / 270) that turn the table at rect so its text reads left to right: the writing
    direction the PDF stores for its text lines, the one with the most characters; 0 without text. A table
    printed sideways then goes to the structure model upright, with its exact PDF words. (An image orientation
    classifier, PP-LCNet_x1_0_doc_ori, turned about 1 in 5 sideways tables upside down; this direction is never
    wrong.)"""
    seen = Counter()
    for b in page.get_text("dict", clip=rect)["blocks"]:
        for l in b.get("lines", []):
            seen[round(-math.degrees(math.atan2(l["dir"][1], l["dir"][0])) / 90) % 4 * 90] +=                 sum(len(s["text"].strip()) for s in l["spans"])
    return seen.most_common(1)[0][0] if seen else 0


def ruled(page, rect, turn, words):
    """The grid of a table printed with ruling lines, from the PDF's own lines -> boxes in objects()' format (rows,
    columns, spanning cells) in the upright frame, or None when the table has no printed grid (fewer than 2 rows or
    2 columns of lines). Lines are thin filled rectangles or line segments; lines closer than GAP points are one
    (double borders). Two neighbouring grid cells with no line between them are one spanning cell. Text beyond the
    outermost lines (a header row above the top line) gets a band of its own, its columns those of the grid.
    words: the table's PDF words in the upright frame."""
    clip, hs, vs = pymupdf.Rect(rect) + (-2, -2, 2, 2), [], []
    for d in page.get_drawings():
        if not d["rect"].intersects(clip):
            continue
        for it in d["items"]:
            b = it[1] if it[0] == "re" else pymupdf.Rect(it[1], it[2]).normalize() if it[0] == "l" else None
            if b is None or not b.intersects(clip):
                continue
            x0, y0, x1, y1 = frame(b, turn)
            if y1 - y0 <= THIN and x1 - x0 > 3:
                hs.append(((y0 + y1) / 2, x0, x1))
            elif x1 - x0 <= THIN and y1 - y0 > 3:
                vs.append(((x0 + x1) / 2, y0, y1))
    ys, xs = lines([h[0] for h in hs]), lines([v[0] for v in vs])
    if len(ys) < 3 or len(xs) < 3:
        return None
    outer = set()  # band edges added for text beyond the outermost lines: every border there counts as drawn
    for axis, lo, hi in ((ys, 1, 3), (xs, 0, 2)):
        if words and min(w[lo] for w in words) < axis[0] - 1:
            axis.insert(0, min(w[lo] for w in words) - 1)
            outer.add((id(axis), 0))
        if words and max(w[hi] for w in words) > axis[-1] + 1:
            axis.append(max(w[hi] for w in words) + 1)
            outer.add((id(axis), len(axis) - 2))
    def drawn(segs, at, a, b):  # a line at `at` covering at least half of [a, b]
        return sum(max(0, min(s[2], b) - max(s[1], a)) for s in segs if abs(s[0] - at) <= GAP) >= 0.5 * (b - a)
    nr, nc = len(ys) - 1, len(xs) - 1
    edge = lambda r, q: (id(ys), r) in outer or (id(xs), q) in outer
    parent = {(r, q): (r, q) for r in range(nr) for q in range(nc)}
    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    for r in range(nr):
        for q in range(nc):
            if q + 1 < nc and not edge(r, q) and not edge(r, q + 1) and not drawn(vs, xs[q + 1], ys[r], ys[r + 1]):
                parent[root((r, q + 1))] = root((r, q))
            if r + 1 < nr and not edge(r, q) and not edge(r + 1, q) and not drawn(hs, ys[r + 1], xs[q], xs[q + 1]):
                parent[root((r + 1, q))] = root((r, q))
    groups = {}
    for k in parent:
        groups.setdefault(root(k), []).append(k)
    objs = [("table row", 1.0, [xs[0], ys[r], xs[-1], ys[r + 1]]) for r in range(nr)]
    objs += [("table column", 1.0, [xs[q], ys[0], xs[q + 1], ys[-1]]) for q in range(nc)]
    for g in groups.values():
        if len(g) > 1:
            r0, r1, q0, q1 = min(r for r, _ in g), max(r for r, _ in g), min(q for _, q in g), max(q for _, q in g)
            objs.append(("table spanning cell", 1.0, [xs[q0], ys[r0], xs[q1 + 1], ys[r1 + 1]]))
    return objs


def lines(positions):
    """Positions of ruling lines (points) -> one per line, sorted: positions within GAP of each other are one line."""
    out = []
    for p in sorted(positions):
        if out and p - out[-1][-1] <= GAP:
            out[-1].append(p)
        else:
            out.append([p])
    return [sum(g) / len(g) for g in out]


def frame(box, turn, back=False):
    """[x0, y0, x1, y1] of a box in PDF points in the upright frame of a table turned by turn degrees, or back
    from it to the page."""
    m = pymupdf.Matrix(turn)
    return list(pymupdf.Rect(box[:4]) * (~m if back else m))


def view(model, page, rect, pad, side, turn=0):
    """One model run on the table cropped with pad points around it; boxes in PDF points (upright frame)."""
    image, (x0, y0, x1, y1) = crop(page, rect, pad, turn)
    return [(l, s, [x0 + b[0] * (x1 - x0), y0 + b[1] * (y1 - y0), x0 + b[2] * (x1 - x0), y0 + b[3] * (y1 - y0)])
            for l, s, b in predict(model, image, side)]


def iou(a, b):
    w, h = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    i = max(w, 0) * max(h, 0)
    return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i) if i else 0


def fuse(runs):
    """Weighted boxes fusion of several runs' boxes: boxes of one class from different runs with IoU >= 0.5 are
    one object, its box their score-weighted mean, its score their summed score / number of runs -- an object
    only some runs see scores low. Kept at THRESHOLD, as a single run's boxes."""
    groups = []  # [label, {run: (score, box)}, fused box]
    for l, s, b, v in sorted(((l, s, b, v) for v, r in enumerate(runs) for l, s, b in r), key=lambda x: -x[1]):
        g = max((g for g in groups if g[0] == l), key=lambda g: iou(g[2], b), default=None)
        if g is None or iou(g[2], b) < 0.5:
            groups.append([l, {v: (s, b)}, b])
        elif v not in g[1]:  # a second box of the same run in one object is a duplicate
            g[1][v] = (s, b)
            w = sum(s for s, _ in g[1].values())
            g[2] = [sum(s * bb[k] for s, bb in g[1].values()) / w for k in range(4)]
    fused = [(l, sum(s for s, _ in seen.values()) / len(runs), box) for l, seen, box in groups]
    return [o for o in fused if o[1] >= THRESHOLD]


def pixels(image, side):
    """Model input [3, h, w]: the image resized to side px on its longest side, normalised as in training."""
    k = side / max(image.size)
    image = image.convert("RGB").resize((round(image.width * k), round(image.height * k)), Image.BILINEAR)
    return torch.from_numpy(((np.asarray(image, np.float32) / 255 - MEAN) / STD).transpose(2, 0, 1).copy())


def predict(model, image, side=1000):
    """[(label, score, [x0, y0, x1, y1] in [0, 1] of the image)] above THRESHOLD, the image resized to side px."""
    with torch.no_grad():
        out = model(pixel_values=pixels(image, side)[None].to(model.device))
    probs = out.logits.softmax(-1)[0, :, :-1]  # last class is "no object"
    scores, labels = probs.max(-1)
    objs = []
    for s, l, (cx, cy, w, h) in zip(scores.tolist(), labels.tolist(), out.pred_boxes[0].tolist()):
        if s >= THRESHOLD:
            objs.append((model.config.id2label[l], s, [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]))
    return objs


def dedupe(boxes, lo, hi):
    """Highest score first; drop a box whose extent along its axis is mostly (> 50 %) inside a kept one.
    Returns (start, end) bands, sorted."""
    # ponytail: the official nms_by_containment (drop rows / columns that win no words) was tried: it dropped real
    # rows and kept extra columns, so plain 1-D NMS stays
    kept = []
    for s, b in sorted(boxes, key=lambda x: -x[0]):
        if all(min(b[hi], k[hi]) - max(b[lo], k[lo]) <= 0.5 * (b[hi] - b[lo]) for _, k in kept):
            kept.append((s, b))
    return sorted((b[lo], b[hi]) for _, b in kept)


def covered(b, bands, lo, hi):
    """Indices of the bands (rows or columns) with at least half their extent inside box b along one axis."""
    return [i for i, (a, z) in enumerate(bands) if min(b[hi], z) - max(b[lo], a) >= 0.5 * (z - a)]


def header_rows(objs, rows):
    """The header rows: the rows the column header boxes cover, contiguous from the top (align_headers)."""
    hits = {r for l, _, b in objs if l == "table column header" for r in covered(b, rows, 1, 3)}
    hits |= set(range(min(hits, default=-1) + 1))  # a header starts at the top row
    header = set()
    while len(header) in hits:
        header.add(len(header))
    return header


def sized(page, words):
    """PyMuPDF words with each one's font size appended (index 8): the size of the text span its centre is in, 0 when
    none. text_rows() tells small print by it; a word box's height is no measure (a "µ" font draws it shorter)."""
    spans = [(s["bbox"], s["size"]) for b in page.get_text("dict")["blocks"] for ln in b.get("lines", [])
             for s in ln["spans"] if s["text"].strip()]
    inside = lambda w, b: b[0] <= (w[0] + w[2]) / 2 <= b[2] and b[1] <= (w[1] + w[3]) / 2 <= b[3]
    return [(*w[:8], next((size for b, size in spans if inside(w, b)), 0)) for w in words]


def text_rows(objs, words):
    """Table Transformer's rows checked against the PDF text layer (words in the same frame as objs); any table:
      * a row is cut where a new printed row starts inside it: every key column (one that almost every body row
        fills) starts a line at the same height, in the table's usual print -- the model read two printed rows as one;
      * the lines at the top of a row, above its first such start, go to the row above while they continue it: small
        print (a "Method: ..." under the row above) or, when the PDF keeps the table's texts in separate text blocks,
        a line of the text block above it in its column (a wrapped name). What is left above the start is a row of
        its own (a section title: "Platelets");
      * so a row with no printed row starting in it whose lines all continue the row above joins it.
    Nothing changes when no printed row starts level across the key columns anywhere (texts not top-aligned).
    Returns objs with the "table row" boxes replaced."""
    get = lambda name: [(s, b) for l, s, b in objs if l == name]
    rows, cols = dedupe(get("table row"), 1, 3), dedupe(get("table column"), 0, 2)
    if not rows or len(cols) < 2 or not words:
        return objs
    header = header_rows(objs, rows)
    size = lambda w: w[8] if len(w) > 8 and w[8] else w[3] - w[1]  # font size (sized()), else the box's height
    usual = float(np.median([size(w) for w in words]))
    centre = lambda w: ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
    col_of = lambda x: min(range(len(cols)), key=lambda q: max(cols[q][0] - x, 0, x - cols[q][1]))
    row_of = lambda y: min(range(len(rows)), key=lambda r: max(rows[r][0] - y, 0, y - rows[r][1]))
    blocks = len({w[5] for w in words}) >= len(rows) / 2  # the PDF keeps the table's texts in separate blocks

    printed, goes_on = [], set()  # (top, column, small print, text blocks, bottom) per printed line in a column
    for q in range(len(cols)):
        group, before = [], None
        for w in sorted((w for w in words if col_of(centre(w)[0]) == q), key=lambda w: w[1]) + [None]:
            if group and (w is None or w[1] - group[0][1] > ROW_TOL):
                line = (min(x[1] for x in group), q, max(map(size, group)) < SMALL * usual,
                        frozenset(x[5] for x in group), max(x[3] for x in group))
                printed.append(line)
                if line[2] or (blocks and before and line[3] & before[3]):
                    goes_on.add(line)  # continues the line above it in its column
                before, group = line, []
            if w is not None:
                group.append(w)
    # key columns by their usual print: a column of small print ("Colorimetric", "Calculated") starts no printed row
    filled = [{x[1] for x in printed if not x[2] and row_of(x[0] + 0.5) == r} for r in range(len(rows))]
    body = [r for r in range(len(rows)) if r not in header and len(filled[r]) >= 2]
    key = {q for q in range(len(cols)) if body and sum(q in filled[r] for r in body) >= KEY * len(body)}
    # a printed row starts where the key columns' lines are level: by their bottoms (baseline), steadier than their
    # tops across fonts (a "µ" sets its word's top 3 points lower); it starts at the highest of those tops
    opens, cluster = {}, []  # line -> the top of the row it opens
    for x in sorted((x for x in printed if not x[2]), key=lambda x: x[4]) + [(0, -1, False, frozenset(), float("inf"))]:
        if cluster and x[4] - cluster[0][4] > ROW_TOL:
            if len(qs := {c[1] for c in cluster}) >= 2 and key <= qs:
                opens.update(dict.fromkeys(cluster, min(c[0] for c in cluster)))
            cluster = []
        cluster.append(x)
    if not opens:
        return objs
    label = min(key, default=0)
    level = lambda x: [y for y in printed if y is not x and abs(y[4] - x[4]) <= ROW_TOL]
    for x in printed:  # a title: a line alone at its height in the label column, in a text block of its own
        if blocks and x[1] == label and not x[2] and x not in goes_on and x not in opens and not level(x):
            opens[x] = x[0]

    # the model's rows made contiguous (a word between two rows went to the nearest), first and last reaching the text
    tops = [min(rows[0][0], min(w[1] for w in words))] + [z for _, z in rows[:-1]]
    bands = list(zip(tops, [z for _, z in rows[:-1]] + [max(rows[-1][1], max(w[3] for w in words))]))
    out = []  # [top, bottom, header]
    for r, (a, z) in enumerate(bands):
        hdr = r in header
        mine = sorted(x for x in printed if a <= x[0] + 0.5 < z)
        k = 0
        if out and not hdr:  # the lines at its top that go on from the row above
            while k < len(mine) and mine[k] in goes_on and mine[k] not in opens:
                k += 1
        if mine and k == len(mine):
            out[-1][1] = z
            continue
        if k:
            a = out[-1][1] = mine[k][0] - 1
        cuts = sorted({opens[x] - 1 for x in mine[k + 1:] if x in opens and opens[x] > mine[k][0] + ROW_TOL})
        # a header row cut apart: only its first part is header (the rest, the first body row printed close under it)
        out += [[p, q, hdr and not i] for i, (p, q) in enumerate(zip([a] + cuts, cuts + [z]))]
    for i in range(len(out) - 1, -1, -1):  # a row with no text in it joins the row below (the last: the row above)
        if len(out) > 1 and not any(out[i][0] <= x[0] + 0.5 < out[i][1] for x in printed):
            if i + 1 < len(out):
                out[i + 1][0] = out[i][0]
            else:
                out[i - 1][1] = out[i][1]
            del out[i]
    x0, x1 = min(c[0] for c in cols), max(c[1] for c in cols)
    keep = [o for o in objs if o[0] != "table row"]
    if header:  # the column header now ends where the header rows do
        heads = [(a, z) for a, z, h in out if h]
        keep = [o for o in keep if o[0] != "table column header"] + (
            [("table column header", 1.0, [x0, heads[0][0], x1, heads[-1][1]])] if heads else [])
    return keep + [("table row", 1.0, [x0, a, x1, z]) for a, z, _ in out]


def text_spans(cells, words):
    """Cells joined into one spanning cell where a printed text runs across a column boundary in its row: Table
    Transformer draws a column line through it when the table's columns change partway down ("Biological Ref.
    Interval", "13.0 - 16.5", "million/cmm" over columns only the rows below use). A text: a line's words less than
    half their height apart, in one print size. Only where nothing else is printed in the columns it covers (else the
    boundary is just drawn a little off, and each word keeps to the cell it overlaps most). Cells that already span
    rows or columns stay as they are. words: in the cells' frame, font size at index 8 when sized()."""
    one = {(c["row"], c["col"]): c for c in cells if c["row_span"] == c["col_span"] == 1}
    if not one or not words:
        return cells
    xs, ys = {}, {}
    for (r, q), c in one.items():
        xs.setdefault(q, (c["bbox_pdf_pt"][0], c["bbox_pdf_pt"][2]))
        ys.setdefault(r, (c["bbox_pdf_pt"][1], c["bbox_pdf_pt"][3]))
    size = lambda w: w[8] if len(w) > 8 and w[8] else w[3] - w[1]
    centre = lambda w: ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
    over = lambda a, b, q: min(b, xs[q][1]) - max(a, xs[q][0])
    joins = {}  # row -> [first column, last column] ranges to join
    for r, (top, bottom) in ys.items():
        mine = [w for w in words if top <= centre(w)[1] < bottom]
        lines = []
        for w in sorted(mine, key=lambda w: w[1]):
            if lines and w[1] - lines[-1][0][1] <= ROW_TOL:
                lines[-1].append(w)
            else:
                lines.append([w])
        texts = []
        for line in lines:
            for k, w in enumerate(sorted(line, key=lambda w: w[0])):
                last = texts[-1][-1] if k else None
                if last and w[0] - last[2] < 0.5 * (w[3] - w[1]) and \
                        abs(size(w) - size(last)) <= 0.15 * max(size(w), size(last)):
                    texts[-1].append(w)
                else:
                    texts.append([w])
        for t in texts:
            a, b = min(w[0] for w in t), max(w[2] for w in t)
            qs = [q for q in xs if over(a, b, q) >= max(4, 0.15 * (b - a))]
            if len(qs) < 2 or any((r, q) not in one for q in range(min(qs), max(qs) + 1)):
                continue
            span = range(min(qs), max(qs) + 1)
            if any(w not in t and xs[span[0]][0] <= centre(w)[0] <= xs[span[-1]][1] for w in mine):
                continue  # something else is printed there: the boundary is only drawn a little off
            for s in joins.setdefault(r, []):
                if s[0] <= span[-1] and span[0] <= s[1]:
                    s[0], s[1] = min(s[0], span[0]), max(s[1], span[-1])
                    break
            else:
                joins[r].append([span[0], span[-1]])
    if not joins:
        return cells
    gone, out = set(), []
    for r, spans in joins.items():
        for a, b in spans:
            parts = [one[(r, q)] for q in range(a, b + 1)]
            gone |= {(r, q) for q in range(a, b + 1)}
            out.append({**parts[0], "col_span": b - a + 1,
                        "bbox_pdf_pt": [parts[0]["bbox_pdf_pt"][0], parts[0]["bbox_pdf_pt"][1],
                                        parts[-1]["bbox_pdf_pt"][2], parts[-1]["bbox_pdf_pt"][3]]})
    out += [c for c in cells if (c["row"], c["col"]) not in gone or c["row_span"] != 1 or c["col_span"] != 1]
    return sorted(out, key=lambda c: (c["row"], c["col"]))


def build_cells(objs):
    """Model boxes (any coordinate space) -> cells in structure.py's format, boxes in the same space. From the
    official refine_table_structure: header rows contiguous from the top (align_headers) and spanning cells
    snapped to the grid, never across the header boundary (align_supercells). Projected row headers only flag
    their row as a section: merging the row lost its values when the model mistook a data row for one."""
    get = lambda name: [(s, b) for l, s, b in objs if l == name]
    rows = dedupe(get("table row"), 1, 3)
    cols = dedupe(get("table column"), 0, 2)
    header = header_rows(objs, rows)
    section = {r for _, b in get("table projected row header") for r in covered(b, rows, 1, 3)}
    own, cells = {}, []
    # ponytail: overlapping spanning cells: the lower-scored one is dropped, official nms_supercells trims it instead
    for _, b in sorted(get("table spanning cell"), key=lambda x: -x[0]):
        rs = covered(b, rows, 1, 3)
        hr, dr = [r for r in rs if r in header], [r for r in rs if r not in header]
        rs = dr if len(dr) > len(hr) else hr  # never across the header boundary: keep the larger side
        qs = covered(b, cols, 0, 2)
        if not rs or not qs:
            continue
        pos = [(r, q) for r in range(rs[0], rs[-1] + 1) for q in range(qs[0], qs[-1] + 1)]
        if len(pos) > 1 and not any(p in own for p in pos):
            own.update(dict.fromkeys(pos, (rs[0], qs[0], rs[-1] - rs[0] + 1, qs[-1] - qs[0] + 1)))
    spans = set(own.values())
    for r in range(len(rows)):
        for q in range(len(cols)):
            if (r, q) not in own:
                spans.add((r, q, 1, 1))
    for r, q, rs, cs in sorted(spans):
        cells.append({"row": r, "col": q, "row_span": rs, "col_span": cs, "column_header": r in header,
                      "row_header": False, "row_section": r in section,
                      "bbox_pdf_pt": [cols[q][0], rows[r][0], cols[q + cs - 1][1], rows[r + rs - 1][1]]})
    return cells, len(rows), len(cols)


def slot(cells, words):
    """Index of the cell each word goes to: the one its box overlaps most, as the official slot_into_containers;
    a word outside every cell goes to the nearest one, so no value is lost."""
    def fit(w, b):
        ov = max(min(w[2], b[2]) - max(w[0], b[0]), 0) * max(min(w[3], b[3]) - max(w[1], b[1]), 0)
        x, y = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        return ov or -(max(b[0] - x, 0, x - b[2]) + max(b[1] - y, 0, y - b[3]))
    return [max(range(len(cells)), key=lambda i: fit(w, cells[i]["bbox_pdf_pt"])) for w in words] if cells else []


def fill(cells, words):
    """Cell text from the PDF text layer (PyMuPDF words), each word in the cell slot() gives it."""
    texts = [[] for _ in cells]
    for w, i in zip(words, slot(cells, words)):
        texts[i].append(w)
    for c, ws in zip(cells, texts):
        c["text"] = " ".join(w[4] for w in sorted(ws, key=lambda w: (w[5], w[6], w[7])))


def selfcheck():
    """3 rows x 3 columns, a spanning header over columns 1-2 of rows 0-1: 6 cells. A duplicate column goes; the
    header box covers row 1 only, yet row 0 is a header row too (a header starts at the top); a spanning cell
    reaching into the data row stays on the header side; a projected row header flags row 2 without merging it."""
    objs = [("table row", .9, [0, 0, 30, 10]), ("table row", .9, [0, 10, 30, 20]), ("table row", .9, [0, 20, 30, 30]),
            ("table column", .9, [0, 0, 10, 30]), ("table column", .9, [10, 0, 20, 30]),
            ("table column", .9, [20, 0, 30, 30]), ("table column", .6, [11, 0, 20, 30]),
            ("table column header", .9, [0, 10, 30, 20]), ("table spanning cell", .9, [10, 0, 30, 25]),
            ("table projected row header", .9, [0, 20, 30, 30])]
    cells, nr, nc = build_cells(objs)
    assert (nr, nc, len(cells)) == (3, 3, 6), (nr, nc, cells)
    span = next(c for c in cells if c["col_span"] == 2)
    assert (span["row"], span["col"], span["row_span"], span["column_header"]) == (0, 1, 2, True), span
    assert [c["column_header"] for c in cells if c["col"] == 0] == [True, True, False]
    assert [c["row_section"] for c in cells if c["row"] == 2] == [True] * 3
    # fusion: a span 3 of 4 runs see (shifted a little) stays, one only 1 run sees goes, a run's duplicate counts once
    runs = [[("table spanning cell", .9, [10 + i, 0, 30, 10]), ("table spanning cell", .9, [0, 20, 30, 30])]
            if i == 0 else [("table spanning cell", .8, [10 + i, 0, 30, 10]), ("table spanning cell", .8, [10, 0, 30, 10])]
            if i < 3 else [] for i in range(4)]
    assert [(round(s, 2), [round(v) for v in b]) for _, s, b in fuse(runs)] == [(0.62, [11, 0, 30, 10])], fuse(runs)
    # agreement: views drawing the same grid agree fully; against a view splitting each of 2 rows in three (rows F1
    # 0, columns 1) and one with a shifted column (rows 1, columns 1/2) -> mean 0.625
    row = lambda y0, y1: ("table row", .9, [0, y0, 30, y1])
    col = lambda x0, x1: ("table column", .9, [x0, 0, x1, 30])
    grid = [row(0, 15), row(15, 30), col(0, 10), col(10, 20)]
    assert agreement([grid, grid, grid]) == 1
    split = [row(5 * i, 5 * i + 5) for i in range(6)] + [col(0, 10), col(10, 20)]
    shifted = [row(0, 15), row(15, 30), col(0, 10), col(16, 26)]
    assert agreement([grid, split, shifted]) == 0.625, agreement([grid, split, shifted])
    # text: whole words by overlap -- two header labels close together stay in their own columns; a word outside
    # every cell goes to the nearest
    w = lambda x0, x1, s, n: (x0, 1, x1, 9, s, 0, 0, n)
    cells = [{"bbox_pdf_pt": [0, 0, 50, 10]}, {"bbox_pdf_pt": [50, 0, 100, 10]}]
    fill(cells, [w(30, 48, "Not", 0), w(52, 70, "Less", 1), w(72, 90, "than", 2), w(104, 110, "6", 3)])
    assert [c["text"] for c in cells] == ["Not", "Less than 6"], cells
    # sideways text (written bottom to top) is turned upright by 90 degrees: its words then run left to right, in
    # reading order, inside the upright crop; frame() goes back to the page
    page = pymupdf.open().new_page()
    page.insert_text((100, 300), "Net income 2025", rotate=90)
    turn, words = upright(page, page.rect), page.get_text("words")
    _, (x0, y0, x1, y1) = crop(page, [90, 180, 110, 310], 5, turn)
    up = [frame(w, turn) for w in words]
    assert turn == 90 and all(x0 <= b[0] < b[2] <= x1 and y0 <= b[1] < b[3] <= y1 for b in up), (turn, up)
    assert sorted(range(3), key=lambda i: up[i][0]) == [0, 1, 2] and up[0][2] - up[0][0] > up[0][3] - up[0][1]
    assert [round(v, 3) for v in frame(up[0], turn, back=True)] == [round(v, 3) for v in words[0][:4]]
    assert upright(pymupdf.open().new_page(), [0, 0, 10, 10]) == 0
    # a page stored rotated (/Rotate) whose sideways text reads upright on screen: for every stored rotation the crop
    # is the table itself, upright, its size the one frame() gives, each word box on its printed ink
    for rot in (0, 90, 180, 270):
        page = pymupdf.open().new_page(width=600, height=800)
        for i in range(3):
            page.insert_text((100 + 20 * i, 700), f"Row {i} Revenue 1,234", rotate=90)
        page.set_rotation(rot)
        rect = [80, 400, 160, 710]
        turn = upright(page, rect)
        image, (x0, y0, x1, y1) = crop(page, rect, 5, turn)
        k = image.width / (x1 - x0)
        assert abs(image.height - (y1 - y0) * k) <= 2 and image.width > image.height, (rot, image.size)
        b = frame(page.get_text("words", clip=rect)[0], turn)
        ink = image.crop([round((b[0] - x0) * k), round((b[1] - y0) * k), round((b[2] - x0) * k), round((b[3] - y0) * k)])
        assert sum(v < 128 for v in ink.convert("L").getdata()) > 20, rot
    # ruled(): a 3 x 3 grid of thin rectangles, the line between columns 1 and 2 missing in row 0 (a header spanning
    # them), drawn as a double border at the bottom; a word above the top line gets a band of its own
    page = pymupdf.open().new_page()
    for y in (100, 120, 140, 160, 161.5):
        page.draw_rect(pymupdf.Rect(100, y, 400, y + 0.5), fill=(0, 0, 0), width=0)
    for x in (100, 200, 300, 400):
        page.draw_rect(pymupdf.Rect(x, 120 if x == 300 else 100, x + 0.5, 161), fill=(0, 0, 0), width=0)
    page.insert_text((110, 95), "Title")
    words = page.get_text("words")
    cells, nr, nc = build_cells(ruled(page, [90, 80, 410, 170], 0, words))
    assert (nr, nc) == (4, 3), (nr, nc)  # title band + 3 drawn rows (the double border is one line)
    assert [(c["row"], c["col"], c["col_span"]) for c in cells if c["col_span"] > 1] == [(1, 1, 2)], cells
    assert ruled(pymupdf.open().new_page(), [0, 0, 100, 100], 0, []) is None  # no lines: no grid
    # text_rows(): the model read Platelet Count + MPV and PDW + PCT as one row each, and a last row of only a small
    # "Method" line. Each printed row starts level in all 4 key columns: those rows are cut there, the small line
    # joins the row above. A section row ("Platelets") stays; a row whose name and range both wrap
    # ("(Serum)" in small print, "Borderline" in the range) is not cut: the value and unit columns start no line there.
    cols = [[0, 100], [100, 150], [150, 200], [200, 300]]
    big = lambda y, *texts: [(cols[q][0] + 2, y, cols[q][0] + 40, y + 10, s, 0, 0, 0) for q, s in enumerate(texts) if s]
    small = lambda y, q, s: [(cols[q][0] + 2, y, cols[q][0] + 40, y + 8, s, 0, 0, 0)]
    words = (big(2, "Test", "Value", "Unit", "Range") + big(17, "Monocyte", "210", "/uL", "200-1000") + small(28, 0, "M1") +
             big(42, "Platelets") + big(64, "Platelet", "279000", "/uL", "150-410") + small(75, 0, "M2") +
             big(85, "MPV", "9.1", "fL", "6-12") + small(96, 0, "M3") + big(106, "PDW", "16.0", "fL", "9-15") +
             small(117, 0, "M4") + big(127, "PCT", "0.25", "%", "0.2-0.4") + small(141, 0, "M5") +
             big(154, "Trig", "180", "mg", "<150") + small(166, 0, "(Serum)") + big(166, "", "", "", "Borderline"))
    bands = [(0, 15), (15, 40), (40, 62), (62, 100), (100, 140), (140, 152), (152, 190)]
    objs = ([("table row", .9, [0, a, 300, z]) for a, z in bands] + [("table column", .9, [a, 0, z, 190]) for a, z in cols]
            + [("table column header", .9, [0, 0, 300, 15])])
    cells, nr, nc = build_cells(text_rows(objs, words))
    fill(cells, words)
    assert (nr, nc) == (8, 4), (nr, nc)
    assert [c["text"] for c in cells if c["col"] == 0] == ["Test", "Monocyte M1", "Platelets", "Platelet M2", "MPV M3",
                                                          "PDW M4", "PCT M5", "Trig (Serum)"], cells
    assert [c["text"] for c in cells if c["col"] == 1][3:7] == ["279000", "9.1", "16.0", "0.25"]
    assert [c["column_header"] for c in cells if c["col"] == 0] == [True] + [False] * 7
    assert text_rows(objs, []) == objs  # no text layer: the model's rows
    # the model's header row holds two printed rows (cut, only the first stays header); the next row starts with the small
    # "Method" line of the row above (goes up), then a section title in a text block of its own (a row of its own),
    # then the printed row; a wrapped name in the same text block as the line above it joins that row
    words = [(*w[:5], n, 0, 0) for n, w in enumerate(
        big(2, "Mono", "210", "/uL", "200") + small(13, 0, "M1") + big(23, "Baso", "0", "/uL", "<200") +
        small(34, 0, "M2") + big(45, "Platelets") + big(56, "Count", "279000", "/uL", "150-410") +
        small(67, 0, "M3") + big(78, "Mean Hb", "32.6", "g/dL", "31-34"))]
    words += [(2, 89, 40, 99, "Conc", words[-4][5], 0, 0), (2, 100, 40, 108, "M4", 99, 0, 0)]  # block of "Mean Hb"
    bands = [(0, 33), (33, 75), (75, 88), (88, 110)]
    objs = ([("table row", .9, [0, a, 300, z]) for a, z in bands] + [("table column", .9, [a, 0, z, 110]) for a, z in cols]
            + [("table column header", .9, [0, 0, 300, 33])])
    cells, nr, nc = build_cells(text_rows(objs, words))
    fill(cells, words)
    assert [c["text"] for c in cells if c["col"] == 0] == ["Mono M1", "Baso M2", "Platelets", "Count M3",
                                                          "Mean Hb Conc M4"], cells
    assert [c["column_header"] for c in cells if c["col"] == 0] == [True, False, False, False, False]
    # text_rows(): a small-print column (methods) is no key column; a header row holding the first body row printed
    # close under it ("Test" / "Hemoglobin") is cut, and only its first part stays header
    words = (big(2, "Test", "", "Result", "Range") + big(14, "Hb", "", "14.5", "13-16") + small(15, 1, "Colorimetric") +
             big(30, "RBC", "", "4.79", "4.5-5.5") + small(31, 1, "Electrical") +
             big(46, "MCV", "", "90.3", "83-101") + small(47, 1, "Derived"))
    bands = [(0, 26), (26, 42), (42, 60)]
    objs = ([("table row", .9, [0, a, 300, z]) for a, z in bands] + [("table column", .9, [a, 0, z, 60]) for a, z in cols]
            + [("table column header", .9, [0, 0, 300, 26])])
    cells, nr, nc = build_cells(text_rows(objs, words))
    fill(cells, words)
    assert [(c["text"], c["column_header"]) for c in cells if c["col"] == 0] == \
           [("Test", True), ("Hb", False), ("RBC", False), ("MCV", False)], cells
    # text_spans(): a column line drawn through "13.0 - 16.5" and "million/cmm" (columns only lower rows use) joins
    # those cells; where the next column has a text of its own, or the run changes print size ("analysis" small,
    # "H 10570" not), nothing is joined
    cols5 = [[0, 100], [100, 150], [150, 200], [200, 260], [260, 300]]
    w = lambda x0, x1, y, s, h=10: (x0, y, x1, y + h, s, 0, 0, 0)
    words = [w(5, 40, 2, "Hb"), w(105, 130, 2, "14.5"), w(205, 235, 2, "13.0"), w(238, 242, 2, "-"),
             w(245, 275, 2, "16.5"), w(5, 40, 22, "Neut"), w(105, 130, 22, "73"), w(205, 235, 22, "40-80"),
             w(265, 290, 22, "7716"), w(5, 30, 42, "WBC"), w(40, 99, 42, "analysis", 7), w(101, 104, 42, "H"),
             w(106, 140, 42, "10570")]
    cells = [{"row": r, "col": q, "row_span": 1, "col_span": 1, "column_header": False, "row_header": False,
              "row_section": False, "bbox_pdf_pt": [a, 20 * r, z, 20 * r + 20]} for r in range(3) for q, (a, z) in enumerate(cols5)]
    cells = text_spans(cells, words)
    fill(cells, words)
    row = lambda r: [(c["col"], c["col_span"], c["text"]) for c in cells if c["row"] == r]
    assert row(0) == [(0, 1, "Hb"), (1, 1, "14.5"), (2, 1, ""), (3, 2, "13.0 - 16.5")], row(0)
    assert row(1) == [(0, 1, "Neut"), (1, 1, "73"), (2, 1, ""), (3, 1, "40-80"), (4, 1, "7716")], row(1)
    assert [c["col_span"] for c in cells if c["row"] == 2] == [1] * 5, row(2)


def run(pdf, pages, adjust=None, name="tatr", model_name=MODEL):
    """adjust(objs, words, page, rect) -> (objs, words): optional change to the model boxes and PDF words before
    the grid is built; the result goes to output/<stem>/structure/<stem>_structure_<name>.json."""
    from app import safe_stem
    stem = safe_stem(Path(pdf).name)
    result = json.loads(tables_json(stem).read_text(encoding="utf-8"))
    model = load()
    doc, out, t0 = pymupdf.open(pdf), [], time.time()
    for p in result["pages"]:
        if p["page"] not in pages:
            continue
        page = doc[p["page"] - 1]
        for d in p["detections"]:
            if not d["is_table"]:
                continue
            rect = [round(c) for c in d["bbox_pdf_pt"]]
            t = time.time()
            objs = objects(model, page, rect)
            words = sized(page, page.get_text("words", clip=rect))
            if adjust:
                objs, words = adjust(objs, words, page, rect)
            cells, nrows, ncols = build_cells(text_rows(objs, words))
            cells = text_spans(cells, words)
            for c in cells:
                c["bbox_pdf_pt"] = [round(v, 2) for v in c["bbox_pdf_pt"]]
            fill(cells, words)
            s = time.time() - t
            out.append({"page": p["page"], "table_id": d["id"], "bbox_pdf_pt": rect, "seconds": round(s, 2),
                        "complete": True, "num_rows": nrows, "num_cols": ncols, "cells": cells})
            heads = [c["text"] for c in cells if c["column_header"] and c["text"]]
            print(f"  p{p['page']} #{d['id']}: {nrows}×{ncols} · {len(cells)} cells · "
                  f"{sum(c['col_span'] > 1 for c in cells)} spanning · {s:.1f} s\n    header: {heads}\n    raw boxes: "
                  + ", ".join(f"{n} {l}" for l, n in Counter(l for l, _, _ in objs).items()))
    path = structure_json(stem, f"_{name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"file": result["file"], "tables_json": tables_json(stem).relative_to(OUT).as_posix(), "model": model_name,
                                "seconds": round(time.time() - t0, 1), "tables": out}, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    print(f"Done · saved {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", type=parse_pages, required=True, help="e.g. 273-275 or 12,15,20-22")
    a = ap.parse_args()
    selfcheck()
    run(a.pdf, a.pages)
