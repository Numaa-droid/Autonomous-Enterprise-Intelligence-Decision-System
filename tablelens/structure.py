"""TableFormerV2 (docling-project/TableFormerV2) table structure on the tables app.py found, via ONNX Runtime.

    python structure.py export                                  # once: model -> models/tableformer_v2/*.onnx
    python structure.py run <pdf> --pages 10-12 [--workers 2] [--verify]

Tables come from output/<name>/table/<name>_tables.json (written by app.py); each is cropped from the original PDF at
144 dpi, as Docling does. The model runs as three ONNX graphs: encoder (table image -> 14x14 features),
decoder step (one OTSL token, with a cache of each layer's earlier inputs) and cell-box head. Tables are
spread over `workers` processes, each with its own sessions and a share of the CPU threads.
Writes output/<name>/structure/<name>_structure.json. --verify also runs the official PyTorch model and compares.
"""

import argparse
import io
import json
import multiprocessing
import os
import statistics
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
import pymupdf
from PIL import Image

REPO = "docling-project/TableFormerV2"
ONNX_DIR = Path(__file__).with_name("models") / "tableformer_v2"
OUT = Path(__file__).with_name("output")  # one folder per PDF: <stem>/pages/pNNN.jpg, table/, structure/


def tables_json(stem):
    """output/<stem>/table/<stem>_tables.json: app.py's detection result."""
    return OUT / stem / "table" / f"{stem}_tables.json"


def structure_json(stem, suffix=""):
    """output/<stem>/structure/<stem>_structure<suffix>.json: a recognition result."""
    return OUT / stem / "structure" / f"{stem}_structure{suffix}.json"


TAGS = ["<pad>", "[UNK]", "<start>", "<end>", "ecel", "fcel", "lcel", "ucel", "xcel", "nl", "ched", "rhed", "srow"]
BOS, EOS = 2, 3
CELLS = {4, 5, 10, 11, 12}  # tokens that are a cell with a box: ecel fcel ched rhed srow (config.data_cells)
CELL_TAGS = {TAGS[i] for i in CELLS}
MAX_LEN = 512               # generated tokens, as Docling
SCALE = 2.0                 # crop at 144 dpi, as Docling
MEAN, STD = np.array([0.485, 0.456, 0.406], np.float32), np.array([0.229, 0.224, 0.225], np.float32)


# ---------- export (needs torch + docling-ibm-models; run once) ----------

def export():
    import torch
    from docling_ibm_models.tableformer_v2 import TableFormerV2
    from docling_ibm_models.tableformer_v2.model import _cxcywh_to_xyxy

    m = TableFormerV2.from_pretrained(REPO).eval().requires_grad_(False)  # weights become graph constants
    torch.backends.mha.set_fastpath_enabled(False)  # fused attention kernels have no ONNX export
    layers, dim = len(m.transformer_decoder.layers), m.config.embed_dim

    class Encoder(torch.nn.Module):
        def forward(self, image):
            return m.encode_images(image)["last_hidden_state"]

    class DecoderStep(torch.nn.Module):
        """One token. The cache holds each layer's earlier inputs (the model's k = v = layer input)."""
        def forward(self, token, pos, memory, past):
            x = m.input_embedding(token) + m.positional_encoding[0].index_select(0, pos)
            new = []
            for i, layer in enumerate(m.transformer_decoder.layers):
                new.append(torch.cat([past[i], x], 1))
                x, _ = layer(x, memory, past_key_value=(past[i], past[i]), use_cache=False)
            return m.output_projection(x)[:, -1], x[:, -1], torch.stack(new)

    def attend(mha, q, kv):
        """nn.MultiheadAttention on one sequence, (L, D) x (S, D) -> (L, D), written out so that the
        exporter keeps L dynamic (its own reshape bakes in the cell count seen at export)."""
        heads, d = mha.num_heads, mha.embed_dim
        wq, wk, wv = mha.in_proj_weight.chunk(3)
        bq, bk, bv = mha.in_proj_bias.chunk(3)
        split = lambda x, w, b: torch.nn.functional.linear(x, w, b).reshape(-1, heads, d // heads).transpose(0, 1)
        q, k, v = split(q, wq, bq), split(kv, wk, bk), split(kv, wv, bv)
        a = torch.softmax(q @ k.transpose(1, 2) / (d // heads) ** 0.5, -1) @ v
        return mha.out_proj(a.transpose(0, 1).reshape(-1, d))

    class BoxHead(torch.nn.Module):
        """BboxHead for one table: cells attend to each other and, independently, to the same memory."""
        def forward(self, cells, memory):
            h = m.bbox_head
            x, mem = h.input_norm(h.input_proj(cells)), h.kv_norm(h.kv_proj(memory[0]))
            for l in h.layers:
                x = l.self_attn_norm(x + attend(l.self_attn, x, x))
                x = l.cross_attn_norm(x + attend(l.cross_attn, x, mem))
                x = l.ffn_norm(x + l.ffn(x))
            return _cxcywh_to_xyxy(torch.sigmoid(h.bbox_mlp(x)))

    ONNX_DIR.mkdir(parents=True, exist_ok=True)
    image = torch.randn(1, 3, 448, 448)
    with torch.no_grad():
        memory = Encoder()(image)
        graphs = [
            ("encoder", Encoder(), (image,), ["image"], ["memory"], None),
            ("decoder_step", DecoderStep(),
             (torch.tensor([[BOS]]), torch.tensor([3]), memory, torch.randn(layers, 1, 3, dim)),
             ["token", "pos", "memory", "past"], ["logits", "hidden", "present"],
             {"past": {2: "past_len"}, "present": {2: "len"}}),
            ("bbox_head", BoxHead(), (torch.randn(7, dim), memory), ["cells", "memory"], ["boxes"],
             {"cells": {0: "cells"}, "boxes": {0: "cells"}}),
        ]
        for name, module, args, ins, outs, axes in graphs:
            torch.onnx.export(module, args, ONNX_DIR / f"{name}.onnx", input_names=ins, output_names=outs,
                              dynamic_axes=axes, opset_version=17, dynamo=False)
            print("exported", ONNX_DIR / f"{name}.onnx")


# ---------- inference (onnxruntime + numpy only) ----------

SESSIONS = None


def init_worker(threads):
    global SESSIONS
    opts = ort.SessionOptions()
    opts.intra_op_num_threads, opts.inter_op_num_threads = threads, 1
    SESSIONS = [ort.InferenceSession(str(ONNX_DIR / f"{n}.onnx"), opts, providers=["CPUExecutionProvider"])
                for n in ("encoder", "decoder_step", "bbox_head")]


def preprocess(image):
    """torchvision Resize((448, 448)) + ToTensor + Normalize, as Docling."""
    x = np.asarray(image.convert("RGB").resize((448, 448), Image.BILINEAR), np.float32) / 255
    return ((x - MEAN) / STD).transpose(2, 0, 1)[None]


def predict(image):
    """(OTSL tags, cell boxes xyxy in [0, 1] of the crop, complete) for one table image.
    complete is False when the model hit MAX_LEN without ending the table: its structure is cut off."""
    enc, dec, head = SESSIONS
    memory = enc.run(None, {"image": preprocess(image)})[0]
    past = np.zeros((dec.get_inputs()[3].shape[0], 1, 0, memory.shape[2]), np.float32)
    token, ids, hidden = BOS, [], []
    for pos in range(MAX_LEN + 1):  # every generated token is fed once more: its hidden state feeds the box head
        logits, h, past = dec.run(None, {"token": np.array([[token]], np.int64), "pos": np.array([pos % 512], np.int64),
                                         "memory": memory, "past": past})
        if token in CELLS:
            hidden.append(h[0])
        if token == EOS or pos == MAX_LEN:
            break
        token = int(logits[0].argmax())
        ids.append(token)
    boxes = head.run(None, {"cells": np.stack(hidden), "memory": memory})[0] if hidden else np.zeros((0, 4))
    return [TAGS[i] for i in ids if i > EOS], boxes, token == EOS


def work(task):
    key, png = task
    t = time.time()
    return key, *predict(Image.open(io.BytesIO(png))), time.time() - t


def build_cells(tags, boxes, rect):
    """OTSL + boxes -> cells with spans, header flags and PDF-point boxes (Docling's _build_table_cells)."""
    rows, row = [], []
    for t in tags:
        if t == "nl":
            rows.append(row)
            row = []
        else:
            row.append(t)
    rows = [r for r in rows + [row] if r]
    ncols = max(map(len, rows), default=0)
    grid = [r + [""] * (ncols - len(r)) for r in rows]
    x0, y0, x1, y1 = rect
    cells, b = [], 0
    for r, line in enumerate(grid):
        for c, tag in enumerate(line):
            if tag not in CELL_TAGS:
                continue
            box = None
            if b < len(boxes):
                l, t, rr, bb = boxes[b]
                box = [round(x0 + l * (x1 - x0), 2), round(y0 + t * (y1 - y0), 2),
                       round(x0 + rr * (x1 - x0), 2), round(y0 + bb * (y1 - y0), 2)]
                b += 1
            cs = next((k for k in range(c + 1, ncols) if grid[r][k] != "lcel"), ncols) - c
            rs = next((k for k in range(r + 1, len(grid)) if grid[k][c] != "ucel"), len(grid)) - r
            cells.append({"row": r, "col": c, "row_span": rs, "col_span": cs, "column_header": tag == "ched",
                          "row_header": tag == "rhed", "row_section": tag == "srow", "bbox_pdf_pt": box})
    return cells, len(grid), ncols


def intervals(cells, key, span, lo, hi, n):
    """Per row (key="row") or column: median predicted [start, end] of its single-span cells, None if it has none."""
    boxes = [[] for _ in range(n)]
    for c in cells:
        if c["bbox_pdf_pt"] and c[span] == 1 and c[key] < n:
            boxes[c[key]].append(c["bbox_pdf_pt"])
    return [(statistics.median(b[lo] for b in v), statistics.median(b[hi] for b in v)) if v else None for v in boxes]


def pick(a, b, ivs):
    """(index, overlap) of the interval that overlaps [a, b] most; with no overlap, the nearest (overlap = -gap)."""
    return max(((i, min(b, iv[1]) - max(a, iv[0])) for i, iv in enumerate(ivs) if iv), key=lambda x: x[1])


def phrases(words):
    """PDF words -> phrases: runs of words on one text line with gaps narrower than the text height (a space is
    ~1/4 of it, a gap between table columns is wider). A phrase is placed as a whole, so a long label's last
    word cannot drift into the next column."""
    out = []
    for w in sorted(words, key=lambda w: (w[5], w[6], w[7])):
        p = out[-1] if out else None
        if p and p["line"] == (w[5], w[6]) and w[0] - p["box"][2] <= w[3] - w[1]:
            p["words"].append(w)
            p["box"] = [min(p["box"][0], w[0]), min(p["box"][1], w[1]), max(p["box"][2], w[2]), max(p["box"][3], w[3])]
        else:
            out.append({"line": (w[5], w[6]), "words": [w], "box": list(w[:4])})
    return out


def align(cells, words, nrows, ncols):
    """Register the model's grid on the PDF text layer.

    The model decides the structure (rows, columns, spans, headers); its boxes are only roughly placed (it sees
    the table at 448 x 448), off by up to half a row and not by the same amount everywhere. The text layer has
    exact positions. Each phrase goes to the row with the nearest centre and the column it overlaps most (else
    the nearest); then every row centre moves to the median of its own text and every column to the extent of
    its text, and phrases are reassigned, until nothing changes (Lloyd / k-means iterations started from the
    model's grid). A cell's text is its phrases and its box the extent of the text in its rows and columns, so
    boxes line up with values and rows. The model's box is kept as bbox_model_pdf_pt; rows or columns without
    text (and pages without a text layer) keep it.
    """
    rows = intervals(cells, "row", "row_span", 1, 3, nrows)
    cols = intervals(cells, "col", "col_span", 0, 2, ncols)
    texts, rband, cband = [[] for _ in cells], {}, {}
    if any(rows) and any(cols):
        own = {}  # (row, col) -> index of the cell covering it
        for i, c in enumerate(cells):
            for r in range(c["row"], c["row"] + c["row_span"]):
                for q in range(c["col"], c["col"] + c["col_span"]):
                    own.setdefault((r, q), i)
        row_h = statistics.median(r[1] - r[0] for r in rows if r)
        grow = lambda d, k, a, b: d.__setitem__(k, (min(a, d[k][0]), max(b, d[k][1])) if k in d else (a, b))
        ps = phrases(words)
        centre = {r: (iv[0] + iv[1]) / 2 for r, iv in enumerate(rows) if iv}  # row -> centre, refined below
        spans, assign = list(cols), None  # column -> [x0, x1], refined below
        for _ in range(10):
            new, top, bottom = [], min(centre.values()) - row_h, max(centre.values()) + row_h
            for p in ps:
                x0, y0, x1, y1 = p["box"]
                # rows by centre: row boxes differ in height, so overlap favours tall neighbours
                r = min(centre, key=lambda k: abs(centre[k] - (y0 + y1) / 2))
                # beyond the first / last row: a title or footnote inside the crop; between rows text always
                # goes somewhere (a merged row beats lost values)
                far = not top <= (y0 + y1) / 2 <= bottom
                new.append((r, None if far else own.get((r, pick(x0, x1, spans)[0]))))
            if new == assign:
                break
            assign, ys, xs = new, {}, {}
            for p, (r, i) in zip(ps, assign):
                if i is not None:  # spanning cells say nothing about a single row's / column's place
                    if cells[i]["row_span"] == 1:
                        ys.setdefault(r, []).append((p["box"][1] + p["box"][3]) / 2)
                    if cells[i]["col_span"] == 1:
                        grow(xs, cells[i]["col"], p["box"][0], p["box"][2])
            centre.update({r: statistics.median(v) for r, v in ys.items()})
            spans = [xs.get(q, s) for q, s in enumerate(spans)]
        for p, (r, i) in zip(ps, assign):
            if i is None:
                continue
            texts[i] += p["words"]
            x0, y0, x1, y1 = p["box"]
            if cells[i]["row_span"] == 1:
                grow(rband, r, y0, y1)
            if cells[i]["col_span"] == 1:
                grow(cband, cells[i]["col"], x0, x1)
    for c, ws in zip(cells, texts):
        c["text"] = " ".join(w[4] for w in sorted(ws, key=lambda w: (w[5], w[6], w[7])))
        c["bbox_model_pdf_pt"] = c["bbox_pdf_pt"]
        xs = [cband.get(q) or cols[q] for q in range(c["col"], c["col"] + c["col_span"]) if q < ncols]
        ys = [rband.get(r) or rows[r] for r in range(c["row"], c["row"] + c["row_span"]) if r < nrows]
        xs, ys = [x for x in xs if x], [y for y in ys if y]
        if xs and ys:
            c["bbox_pdf_pt"] = [round(min(x[0] for x in xs), 2), round(min(y[0] for y in ys), 2),
                                round(max(x[1] for x in xs), 2), round(max(y[1] for y in ys), 2)]


def selfcheck():
    """A 2x2 grid whose predicted boxes sit left of their values: text still lands in the right cells and the
    boxes move onto it; a long label's last word ("e") stays with its label though it is nearer the value
    column; a footnote far below every row is ignored."""
    cells = [{"row": r, "col": q, "row_span": 1, "col_span": 1, "bbox_pdf_pt": [q * 100, r * 20, q * 100 + 50, r * 20 + 10]}
             for r in range(2) for q in range(2)]
    w = lambda x0, y0, x1, y1, s, n: (x0, y0, x1, y1, s, 0, n, 0)
    words = [w(10, 1, 40, 9, "Sales", 0), w(160, 1, 190, 9, "1.234", 1), w(10, 21, 40, 29, "Costs", 2),
             w(42, 21, 85, 29, "and", 2), w(87, 21, 92, 29, "e", 2), w(160, 21, 190, 29, "(56)", 3),
             w(10, 90, 60, 98, "footnote", 4)]
    align(cells, words, 2, 2)
    assert [c["text"] for c in cells] == ["Sales", "1.234", "Costs and e", "(56)"], cells
    assert cells[1]["bbox_pdf_pt"] == [160, 1, 190, 9] and cells[1]["bbox_model_pdf_pt"] == [100, 0, 150, 10], cells[1]
    assert cells[0]["bbox_pdf_pt"] == [10, 1, 92, 9], cells[0]  # label column spans its longest label

    # a two-line label whose 2nd line (centre 15) is nearer the next row's predicted centre (21) than its own (5):
    # its row's value (centre 11) pulls the row centre down, so the line stays with its label
    cells = [{"row": r, "col": q, "row_span": 1, "col_span": 1, "bbox_pdf_pt": [q * 100, r * 16, q * 100 + 50, r * 16 + 10]}
             for r in range(2) for q in range(2)]
    words = [w(10, 3, 40, 11, "Gains", 0), w(10, 11, 40, 19, "net", 1), w(110, 7, 140, 15, "12", 2),
             w(10, 24, 40, 32, "Tax", 3), w(110, 24, 140, 32, "(3)", 4)]
    align(cells, words, 2, 2)
    assert [c["text"] for c in cells] == ["Gains net", "12", "Tax", "(3)"], [c["text"] for c in cells]


def run(pdf, pages, workers, verify):
    from app import safe_stem
    stem = safe_stem(Path(pdf).name)
    result = json.loads(tables_json(stem).read_text(encoding="utf-8"))
    doc = pymupdf.open(pdf)
    tables = {}  # (page, id) -> crop rect in PDF points, Docling-style (rounded)
    for p in result["pages"]:
        if p["page"] in pages:
            for d in p["detections"]:
                if d["is_table"]:
                    tables[(p["page"], d["id"])] = [round(c) for c in d["bbox_pdf_pt"]]
    # clip in the displayed frame: bbox_pdf_pt is unrotated, get_pixmap's clip is not on /Rotate pages
    tasks = [(k, doc[k[0] - 1].get_pixmap(matrix=pymupdf.Matrix(SCALE, SCALE),
                                          clip=pymupdf.Rect(r) * doc[k[0] - 1].rotation_matrix).tobytes("png"))
             for k, r in tables.items()]
    threads = max(1, (os.cpu_count() - 2) // workers)
    print(f"{len(tasks)} table(s) on pages {min(pages)}-{max(pages)} · {workers} worker(s) × {threads} thread(s)")

    t0 = time.time()
    with multiprocessing.Pool(workers, init_worker, (threads,)) as pool:  # sessions load once per worker
        pool.map(int, range(workers))  # wait until every worker has its sessions, so timing is inference only
        t0 = time.time()
        preds = {k: rest for k, *rest in pool.imap_unordered(work, tasks)}
    seconds = time.time() - t0

    out = []
    for (page, tid), rect in tables.items():
        tags, boxes, complete, s = preds[(page, tid)]
        cells, nrows, ncols = build_cells(tags, boxes, rect)
        align(cells, doc[page - 1].get_text("words", clip=rect), nrows, ncols)
        out.append({"page": page, "table_id": tid, "bbox_pdf_pt": rect, "seconds": round(s, 2), "complete": complete,
                    "num_rows": nrows, "num_cols": ncols, "otsl": " ".join(tags), "cells": cells})
        print(f"  p{page} #{tid}: {nrows}×{ncols} · {len(cells)} cells · "
              f"{sum(c['column_header'] for c in cells)} column-header cells · {s:.1f} s"
              + ("" if complete else f" · INCOMPLETE (hit {MAX_LEN} tokens)"))
    path = structure_json(stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"file": result["file"], "tables_json": tables_json(stem).relative_to(OUT).as_posix(), "model": REPO, "runtime": f"onnxruntime {ort.__version__}",
                                "workers": workers, "threads_per_worker": threads, "seconds": round(seconds, 1),
                                "tables": out}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Done in {seconds:.1f} s (wall) · saved {path}")
    if verify:
        compare(tasks, preds)


def compare(tasks, preds):
    """Official PyTorch TableFormerV2 (Docling's generate) on the same crops: same OTSL, same boxes?"""
    import torch
    from docling_ibm_models.tableformer_v2 import TableFormerV2
    from transformers import AutoTokenizer
    m, tok = TableFormerV2.from_pretrained(REPO).eval(), AutoTokenizer.from_pretrained(REPO)
    t0, same = time.time(), 0
    for key, png in tasks:
        o = m.generate(torch.from_numpy(preprocess(Image.open(io.BytesIO(png)))), tok, max_length=MAX_LEN)
        tags = [TAGS[i] for i in o["generated_ids"][0].tolist() if i > EOS]
        boxes = o["predicted_bboxes"][0].numpy() if o["predicted_bboxes"] is not None else np.zeros((0, 4))
        otags, oboxes = preds[key][:2]
        ok = tags == otags and len(boxes) == len(oboxes)
        diff = float(np.abs(boxes - oboxes).max()) if ok and len(boxes) else 0.0
        same += ok
        print(f"  p{key[0]} #{key[1]}: OTSL {'same' if tags == otags else 'DIFFERENT'} · max box diff {diff:.5f}")
    print(f"PyTorch (1 process, {torch.get_num_threads()} threads): {time.time() - t0:.1f} s · "
          f"{same}/{len(tasks)} tables identical")
    assert same == len(tasks), "ONNX and PyTorch disagree"


def parse_pages(s):
    pages = set()
    for part in s.split(","):
        a, _, b = part.partition("-")
        pages |= set(range(int(a), int(b or a) + 1))
    return pages


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export")
    r = sub.add_parser("run")
    r.add_argument("pdf")
    r.add_argument("--pages", type=parse_pages, required=True, help="e.g. 268-277 or 12,15,20-22")
    r.add_argument("--workers", type=int, default=2)
    r.add_argument("--verify", action="store_true", help="also run the PyTorch model and compare")
    a = ap.parse_args()
    selfcheck()
    if a.cmd == "export":
        export()
    else:
        run(a.pdf, a.pages, a.workers, a.verify)
