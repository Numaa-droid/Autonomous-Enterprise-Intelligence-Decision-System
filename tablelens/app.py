"""Web UI for table + text detection on medical reports: upload a PDF, follow a live log, get boxes + JSON.

    python app.py        # runs the self-check, then serves http://127.0.0.1:8000

Detection and recognition run on the GPU server when one is deployed and reachable (gpu_server.py), otherwise on
this computer (CPU); a job the server cannot take runs here too.

Two learned models, no hand-written layout rules:
  * microsoft/table-transformer-detection proposes tables (low threshold: high recall).
  * docling-project/docling-layout-heron (Apache-2.0, trained on DocLayNet incl. financial reports)
    labels every region of the page: table, text, section header, list item, caption, picture, ...
A table is a region the layout model calls a table; Table Transformer boxing the same region raises
the confidence (noisy-OR of both scores, as independent evidence). Table Transformer boxes on regions
the layout model reads as text / list / picture are reported as rejected. Text regions come from the
layout model, their text from the PDF's text layer. The layout model has no chart class (a chart is a "picture"
to it): docling's DocumentFigureClassifier classifies each picture, and bar / line / pie / scatter / box plots
become "chart" regions.
Recognition is a second job, started from the UI on a finished detection (/recognise): content.py reads each
table's structure (Table Transformer v1.1-all + the text layer, or PaddleOCR-VL-1.6 for sideways tables / scans) and
each chart's data as a table (PaddleOCR-VL-1.6: chart_table); in mode "all" also every other region with its PDF
text-layer text (PaddleOCR-VL-1.6 for scans and pictures; QR codes / barcodes decoded), in the page's reading order. Saved to
output/<name>/structure/, then converted to the reference JSON the LLM reads (reference.py, output/<name>/reference/):
the generic medical-report schema (schema/medical-report.schema.json), every lab test with its flag (medical.py).
One job runs at a time; another waits for it.
"""

import asyncio
import base64
import contextlib
import functools
import gzip
import io
import json
import math
import os
import re
import statistics
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse

import pymupdf
import torch
import torchvision
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from transformers import AutoImageProcessor, RTDetrV2ForObjectDetection, TableTransformerForObjectDetection

import content
from detect import MODEL, to_coco_bbox
from structure import structure_json, tables_json

LAYOUT_MODEL = "docling-project/docling-layout-heron"
CANDIDATE_SCORE = 0.1  # Table Transformer proposals kept for the fusion (the threshold applies after it)
LAYOUT_SCORE = 0.3     # layout regions kept; tables still need the fused score >= threshold
TEXT_SCORE = 0.5       # layout regions reported as text blocks
# text_tables(): a borderless table the layout model reads as one of these classes is a table when its PDF text layer
# is a grid (text_grid): this many lines of 2+ texts, texts more than GRID_GAP points apart
GRID_LABELS = ("key_value_region", "form")
GRID_ROWS = 3
GRID_GAP = 8
# layout classes that are tables: a table of contents is the model's "document_index" class, a table to docling too
TABLES = ("table", "document_index")
TEXT_LABELS = {"text": "paragraph", "section_header": "heading", "title": "title", "list_item": "list item",
               "caption": "caption", "footnote": "footnote", "page_header": "page header", "page_footer": "page footer",
               "picture": "picture", "formula": "formula", "code": "code", "key_value_region": "key-value",
               "form": "form", "document_index": "index", "checkbox_selected": "checkbox", "checkbox_unselected": "checkbox"}

OUT = Path(__file__).with_name("output")  # <name>/pages/pNNN.jpg, <name>/table/, <name>/structure/ per PDF
OUT.mkdir(exist_ok=True)
DOC = Path(__file__).with_name("doc")  # the PDFs; recognition finds a result's PDF here by its name
DOC.mkdir(exist_ok=True)
SERVER = "tablelens-serve"  # the GPU server's app name (gpu_server.py)
EVENTS = "tablelens-events"  # the Modal queue a GPU server job's events come through, one partition per job
OUTAGE = 900  # seconds without a connection to Modal before a GPU server job is given up here
GPU = None  # the deployed GPU server (gpu_server()), or None: detection and recognition run on this computer
SAVE_EVERY = 30  # seconds between _structure.json saves during recognition (the file grows to tens of MB)
MAX_UPLOAD_MB = 500  # a bigger upload is refused (413)
IMAGES = (".jpg", ".jpeg", ".png")  # a report uploaded as a photo / scan image: made a one-page PDF (as_pdf)
IMAGE_WIDTH_IN = 8.27  # an image with no usable DPI is taken as this wide (A4)
MAX_PIXELS = 25_000_000  # a page image has at most this many pixels: the dpi is lowered for a huge page (~75 MB RGB)
# ponytail: one job at a time (one GPU's memory; no two jobs writing the same result); a queue per GPU if users wait
RUN = threading.Lock()
PDF = threading.Lock()  # PyMuPDF is not thread-safe: every use of it (jobs, uploads) holds this

@contextlib.asynccontextmanager
async def lifespan(_):
    global GPU
    # blocking calls in a worker thread, not on the event loop (Modal warns about its blocking API there)
    GPU = await asyncio.to_thread(gpu_server)  # here, not only under __main__: `uvicorn app:app` uses it too
    print("Detection + recognition: " + ("on the GPU server" if GPU else "on this computer (no GPU server reachable)"))
    if os.environ.get("MODELS_ON_GPU") == "1":  # the Docker image: every model on the GPU before the first job
        await asyncio.to_thread(to_gpu)
    yield
    # shutting down (docker stop: SIGTERM): work() stops each job at its next event, so the job's finally saves
    # what it has read; waited for up to 25 s (docker-compose.yml: stop_grace_period 30 s)
    for job in list(JOBS.values()):
        job["seen"] = 0
    if await asyncio.to_thread(RUN.acquire, timeout=25):
        RUN.release()


app = FastAPI(lifespan=lifespan)
app.mount("/output", StaticFiles(directory=OUT), name="output")  # page images for re-opened results


@app.middleware("http")
async def same_origin(request: Request, call_next):
    """A POST from another site's page (a form posts without a CORS preflight) is refused: no jobs started or
    PDFs written from a page the user merely visits."""
    origin = request.headers.get("origin")
    if request.method == "POST" and origin and urlparse(origin).netloc != request.headers.get("host"):
        return Response("Cross-site request refused.", status_code=403)
    return await call_next(request)


def safe_stem(name):
    """Upload file name -> a stem safe to use inside OUT (no path parts, no odd characters). An image keeps its type
    in the stem ("report.jpg" -> report_jpg, its PDF doc/report_jpg.pdf): it does not share a report.pdf's results."""
    p = PureWindowsPath(name or "")
    stem = p.stem + (f"_{p.suffix[1:]}" if p.suffix.lower() in IMAGES else "")
    return re.sub(r"[^\w.-]+", "_", stem).strip("_") or "document"


def write_atomic(path, data):
    """path replaced in one step: a job stopped mid-write (docker stop) leaves the old file, not half of a new one."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    os.replace(tmp, path)


def pdf_info(**src):
    """(needs a password, page count) of a PDF (pymupdf.open arguments); 400 when it does not open."""
    with PDF:
        try:
            with pymupdf.open(**src) as doc:
                return doc.needs_pass, 0 if doc.needs_pass else doc.page_count
        except Exception:
            raise HTTPException(400, "Not a readable PDF file (or JPEG / PNG image).")


def upload(file):
    """The uploaded file as PDF bytes (an image made a one-page PDF: as_pdf); 413 above MAX_UPLOAD_MB."""
    data = file.file.read(MAX_UPLOAD_MB * 1048576 + 1)
    if len(data) > MAX_UPLOAD_MB * 1048576:
        raise HTTPException(413, f"The file is over {MAX_UPLOAD_MB} MB.")
    return as_pdf(data, file.filename)


def as_pdf(data, name):
    """An uploaded file's bytes as a PDF: a PDF as it is; a JPEG / PNG photo or scan of a report as a one-page PDF
    with no text layer, which the pipeline then reads as a scan (PaddleOCR-VL). The image is set upright by its EXIF
    orientation (a phone photo is often stored sideways) and sized by its DPI, else as if it were A4 wide
    (IMAGE_WIDTH_IN), so a page of any pixel size prints at the usual scale. 400 when it is no readable image."""
    if Path(name or "").suffix.lower() not in IMAGES:
        return data
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        image.load()
    except Exception:
        raise HTTPException(400, f"{name} is not a readable JPEG or PNG image.")
    dpi = (image.info.get("dpi") or (0, 0))[0]
    # no DPI, the 72 dpi that cameras and image libraries write by default, or one that gives no paper size: A4 wide
    if not dpi or dpi <= 72 or not 3 <= image.width / dpi <= 17:
        dpi = image.width / IMAGE_WIDTH_IN
    buf = io.BytesIO()  # a PNG stays lossless, a JPEG stays a JPEG
    if Path(name).suffix.lower() == ".png":
        image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB").save(buf, "PNG")
    else:
        image.convert("RGB").save(buf, "JPEG", quality=95)
    with PDF, pymupdf.open() as doc:
        page = doc.new_page(width=image.width * 72 / dpi, height=image.height * 72 / dpi)
        page.insert_image(page.rect, stream=buf.getvalue())
        return doc.tobytes()


def find_pdf(stem):
    """The PDF in doc/ whose name gives this stem ("Annual Report 2025.pdf" -> Annual_Report_2025), or None."""
    return next((p for p in DOC.glob("*.pdf") if safe_stem(p.name) == stem), None)


@functools.cache
def load():
    return (AutoImageProcessor.from_pretrained(MODEL), TableTransformerForObjectDetection.from_pretrained(MODEL).eval(),
            AutoImageProcessor.from_pretrained(LAYOUT_MODEL), RTDetrV2ForObjectDetection.from_pretrained(LAYOUT_MODEL).eval())


def to_gpu():
    """Every model on the GPU and PaddleOCR-VL (llama-server) started up front, once: on the GPU server
    (gpu_server.py) and in the Docker image (MODELS_ON_GPU=1). The loaders are cached, so the jobs get these."""
    _, dmodel, _, lmodel = load()
    for m in (dmodel, lmodel, content.structure_tatr.load(content.STRUCTURE_MODEL),
              content.structure_tatr.load(content.structure_tatr.MODEL),
              content.figure_model()[1]):  # picture / chart classifier (detection)
        m.to("cuda")
    try:
        content.vlm_server()  # PaddleOCR-VL on this machine's GPU (content.VLM_URL: 127.0.0.1)
    except Exception as e:  # detection and text-layer tables need no VLM: a job tries again on its first chart / scan
        print(f"{content.VLM} not started: {type(e).__name__}: {e}", flush=True)


def predict(processor, model, image, threshold):
    """[(score, label, [x0, y0, x1, y1] px)], best first; same-label near-duplicates (IoU > 0.5) dropped."""
    with torch.inference_mode():
        outputs = model(**processor(images=image, return_tensors="pt").to(model.device))  # the GPU on the GPU server
    r = processor.post_process_object_detection(outputs, threshold=threshold, target_sizes=[(image.height, image.width)])[0]
    keep = torchvision.ops.batched_nms(r["boxes"], r["scores"], r["labels"], 0.5).tolist()
    return [(r["scores"][i].item(), model.config.id2label[r["labels"][i].item()], r["boxes"][i].tolist()) for i in keep]


def area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def inter(a, b):
    return area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])


def cover(a, b):
    """Share of the smaller box that lies inside the other."""
    return inter(a, b) / (min(area(a), area(b)) or 1)


def fuse(tatr, layout, threshold):
    """Tables from both models' boxes (pixels).

    tables: layout-model tables (TABLES classes), confidence = noisy-OR of the layout score and the best Table
    Transformer box on the same region; a table mostly inside a more confident one is a duplicate.
    rejected: Table Transformer boxes >= threshold on regions the layout model does not call a table,
    with the layout class it sees there instead.
    """
    tables, used = [], set()
    for h, label, box in sorted((x for x in layout if x[1] in TABLES), reverse=True):
        hits = [(tatr[i][0], i) for i in range(len(tatr)) if cover(box, tatr[i][2]) >= 0.7]
        used |= {i for _, i in hits}
        t, i = max(hits, default=(0.0, None))
        score = 1 - (1 - h) * (1 - t)
        if score >= threshold and not any(cover(box, k["box"]) >= 0.7 for k in tables):
            tables.append({"score": score, "layout_label": label, "layout_score": h, "tatr_score": None if i is None else t,
                           "box": box, "tatr_box": None if i is None else tatr[i][2]})
    rejected = []
    for i, (t, _, tbox) in enumerate(tatr):
        if t >= threshold and i not in used:
            seen = Counter()
            for _, label, box in layout:
                seen[label] += inter(box, tbox)
            rejected.append({"score": t, "tatr_box": tbox, "seen_as": seen.most_common(1)[0][0] if +seen else None})
    return tables, rejected


def text_grid(words):
    """True when PDF words (one region) are laid out as a table: GRID_ROWS or more lines of two or more separately
    printed texts (more than GRID_GAP apart), 3 or more columns that 2 or more of those lines share, and figures
    (a digit outside the first column) in 2 or more of them -- a signature block or a form of labels has none. A
    label / value form (a patient banner: "Name | : Jane", "Collected On | : 16-Sep-2025") is no table either: a
    third or more of its texts start or end with ":", or are a ":" printed on its own."""
    lines = []
    for w in sorted(words, key=lambda w: w[1]):
        if lines and w[1] - lines[-1][0][1] <= 3:
            lines[-1].append(w)
        else:
            lines.append([w])
    rows, everything = [], []
    for line in lines:
        texts = []
        for w in sorted(line, key=lambda w: w[0]):
            if texts and w[0] - texts[-1][-1][2] < GRID_GAP:
                texts[-1].append(w)
            else:
                texts.append([w])
        everything += [" ".join(x[4] for x in t) for t in texts]
        if len(texts) >= 2:
            rows.append([(t[0][0], max(x[2] for x in t), " ".join(x[4] for x in t)) for t in texts])
    if len(rows) < GRID_ROWS:
        return False
    cols = []  # [x0, x1, rows]: texts whose spans overlap, line to line, are one column
    for a, b, i in sorted((a, b, i) for i, r in enumerate(rows) for a, b, _ in r):
        if cols and a <= cols[-1][1]:
            cols[-1][1] = max(cols[-1][1], b)
            cols[-1][2].add(i)
        else:
            cols.append([a, b, {i}])
    figures = sum(any(re.search(r"\d", s) for _, _, s in r[1:]) for r in rows)
    # label colons: "Name" ": Jane", "Sample Type :", or a ":" printed on its own between label and value
    labels = sum(s.startswith(":") or s.endswith(":") for s in everything)
    return sum(len(c[2]) >= 2 for c in cols) >= 3 and figures >= 2 and labels < len(everything) / 3


def text_tables(page, dpi, layout, rejected, tables):
    """Tables neither model calls one: a borderless table the layout model reads as a key-value region or form (or a
    Table Transformer box it rejected), whose PDF text layer is a grid (text_grid). In fuse()'s format, plus the
    reason; the rejected boxes promoted are taken out of `rejected`. Pages stored rotated are left as they are."""
    if page.rotation:
        return []
    found = []
    cands = [(h, label, box) for h, label, box in layout if label in GRID_LABELS and h >= TEXT_SCORE]
    cands += [(r["score"], f"rejected:{r['seen_as']}", r["tatr_box"]) for r in rejected]
    for h, label, box in sorted(cands, key=lambda c: -area(c[2])):
        if any(cover(box, k["box"]) >= 0.5 for k in tables + found):
            continue
        clip = pymupdf.Rect([c * 72 / dpi for c in box])
        words = [w for w in page.get_text("words", clip=clip) if content.clean(w[4])]
        if not text_grid(words):
            continue
        found.append({"score": h, "layout_label": label, "layout_score": h, "tatr_score": None, "box": box,
                      "tatr_box": None, "reason": f"PDF text layer: a grid of aligned columns (layout model: "
                                                  f"{TEXT_LABELS.get(label.split(':')[-1], label.split(':')[-1])})"})
        if label.startswith("rejected:"):
            rejected[:] = [r for r in rejected if r["tatr_box"] is not box]
    return found


def geometry(box, page, dpi):
    """bbox / xyxy in displayed pixels at dpi, and the unrotated PDF rect (usable as clip= in PyMuPDF)."""
    xyxy = [round(c, 2) for c in box]
    pdf = pymupdf.Rect([c * 72 / dpi for c in box]) * page.derotation_matrix
    return {"bbox": to_coco_bbox(*xyxy), "bbox_xyxy": xyxy, "bbox_pdf_pt": [round(c, 2) for c in pdf]}


def analyse_page(page, image, tatr, layout, threshold, dpi):
    """(detections, text_blocks) for one page from both models' outputs. Tables and text blocks are numbered in
    reading order (content.page_order: column by column) and carry their place in it ("order"); a table gets the
    heading right before it as its title (+ subtitle, content.table_titles). Rejected boxes come after, top to bottom."""
    tables, rejected = fuse(tatr, layout, threshold)
    tables += text_tables(page, dpi, layout, rejected, tables)  # borderless tables neither model calls one
    dets = [{"label": "table", "is_table": True, "score": round(k["score"], 4), "layout_score": round(k["layout_score"], 4),
             "tatr_score": None if k["tatr_score"] is None else round(k["tatr_score"], 4), **geometry(k["box"], page, dpi),
             "bbox_model": None if k["tatr_box"] is None else to_coco_bbox(*k["tatr_box"]),
             "reason": k.get("reason") or f"layout model: {TEXT_LABELS.get(k['layout_label'], k['layout_label'])} "
                       f"{k['layout_score']:.0%} · Table Transformer: "
                       + ("no box (missed it)" if k["tatr_score"] is None else f"{k['tatr_score']:.0%}")} for k in tables]
    rejects = sorted([{"label": "table", "is_table": False, "score": round(r["score"], 4), "layout_score": None,
                       "tatr_score": round(r["score"], 4), **geometry(r["tatr_box"], page, dpi), "bbox_model": to_coco_bbox(*r["tatr_box"]),
                       "reason": f"layout model reads this region as {TEXT_LABELS.get(r['seen_as'], 'nothing')}, not a table"}
                      for r in rejected], key=lambda d: (d["bbox"][1], d["bbox"][0]))

    items = [("table", d) for d in dets]  # (layout / docling label, detection or text block)
    for s, label, box in layout:
        if label == "table" or s < TEXT_SCORE or any(inter(box, k["box"]) >= 0.7 * area(box) for k in tables):
            continue  # text inside a table is the table's content
        clip = pymupdf.Rect([c * 72 / dpi for c in box]) * page.derotation_matrix
        text = " ".join(content.clean(page.get_text("text", clip=clip)).split())
        items.append((label, {"label": TEXT_LABELS.get(label, label), "score": round(s, 4), **geometry(box, page, dpi),
                              "words": len(text.split()), "text": text}))
    # the layout model has no chart class: docling's figure classifier tells charts apart (the page's pictures in one batch)
    pics = [i for i, (l, _) in enumerate(items) if l == "picture"]
    for i, (figure, fs) in zip(pics, content.figure_classes([image.crop(items[i][1]["bbox_xyxy"]) for i in pics])):
        label = "chart" if figure in content.CHARTS else "picture"
        items[i] = (label, {**items[i][1], "label": label, "figure_class": figure, "figure_score": round(fs, 4)})

    # reading order on the displayed page image (pixels, top-left origin): page header, columns, page footer
    seq = [items[i] for i in content.page_order((image.width, image.height), [(l, x["bbox_xyxy"]) for l, x in items])]
    titles = content.table_titles([(l, x["bbox_xyxy"], x.get("words", 0)) for l, x in seq])
    seq = [(l, {**x, "order": n}) for n, (l, x) in enumerate(seq)]
    dets = [{"id": i, **d} for i, d in enumerate([x for l, x in seq if l == "table"] + rejects, 1)]
    blocks = [{"id": i, **b} for i, b in enumerate([x for l, x in seq if l != "table"], 1)]
    at = {x["order"]: x for x in dets + blocks if "order" in x}
    for t, (ti, si) in titles.items():
        if not at[ti]["text"]:  # a heading with no text layer is no title: the UI would hide it and show none
            continue
        at[t].update(title=at[ti]["text"], title_id=at[ti]["id"])
        if si is not None:
            at[t].update(subtitle=at[si]["text"], subtitle_id=at[si]["id"])
    return dets, blocks


def render(page, dpi):
    """(image, dpi used): the dpi lowered for a huge page, so the image has at most MAX_PIXELS pixels."""
    pdpi = max(1, min(dpi, int(72 * math.sqrt(MAX_PIXELS / max(page.rect.width * page.rect.height, 1)))))
    pix = page.get_pixmap(dpi=pdpi)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), pdpi


def jpeg(image):
    img = image.copy()
    img.thumbnail((1100, 100_000))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def log(msg, level="info", page=None, stage=None, since=None, tag=None):
    """Activity-log event for the UI. level: info | ok | warn | err; stage: render | model | check;
    tag: table | chart | picture | text (the UI colours those lines)."""
    return {"type": "log", "level": level, "page": page, "stage": stage, "msg": msg,
            "dur": round(time.time() - since, 2) if since else None, "tag": tag}


def detection_log(d, page_no):
    head = f"#{d['id']} table · {d['score']:.0%}"
    if not d["is_table"]:
        return log(f"Table Transformer box {d['score']:.0%} → rejected: {d['reason']}", "warn", page_no, "check")
    agree = "both models agree" if d["tatr_score"] is not None else "found by the layout model (Table Transformer missed it)"
    title = (f" · title: {d['title']!r}" + (f" / {d['subtitle']!r}" if d.get("subtitle") else "")) if d.get("title") else ""
    return log(f"{head} → table ✓ {agree} ({d['reason']}){title}", "ok", page_no, "check", tag="table")


def run(doc, name, threshold, dpi, size=0):
    try:
        t0, scores, pages, results = time.time(), [], [], []
        stem = safe_stem(name)
        (OUT / stem / "pages").mkdir(parents=True, exist_ok=True)
        rotated = sum(p.rotation != 0 for p in doc)
        yield log(f"Received {name} · {size / 1048576:.1f} MB · {doc.page_count} pages"
                  + (f" · {rotated} stored rotated" if rotated else ""), "ok")
        if hidden := content.printed_only(doc):
            where = ("every page" if len(hidden) == doc.page_count
                     else "page(s) " + ", ".join(map(str, hidden[:20])) + (" …" if len(hidden) > 20 else ""))
            yield log(f"Scanned with a hidden OCR text layer ({where}): that text is not used, these pages are read "
                      f"from their image like any scan ({content.VLM})", "warn")
        yield {"type": "meta", "file": name, "pages": doc.page_count, "model": MODEL, "layout_model": LAYOUT_MODEL,
               "threshold": threshold, "dpi": dpi}
        t = time.time()
        yield log(f"Loading {MODEL} + {LAYOUT_MODEL} + {content.FIGURE_MODEL} (chart detection) …", stage="model")
        dproc, dmodel, lproc, lmodel = load()
        content.figure_model()
        yield log("Models ready (already in memory)" if time.time() - t < 0.05 else "Models loaded on CPU",
                  "ok", stage="model", since=t)
        for page_no, page in enumerate(doc, 1):
            t = time.time()
            image, pdpi = render(page, dpi)
            yield log(f"Rendered {image.width}×{image.height} px @ {pdpi} dpi"
                      + (f" (lowered from {dpi}: a {page.rect.width / 72:.0f}×{page.rect.height / 72:.0f} in page)"
                         if pdpi < dpi else "")
                      + (f" · page stored rotated {page.rotation}°" if page.rotation else ""),
                      page=page_no, stage="render", since=t)
            t = time.time()
            tatr = predict(dproc, dmodel, image, CANDIDATE_SCORE)
            boxes = [s for s, _, _ in tatr if s >= threshold]
            yield log(f"Table Transformer → {len(boxes)} box(es) ≥ {threshold:.2f} · {len(tatr) - len(boxes)} weaker candidate(s)",
                      page=page_no, stage="model", since=t)
            t = time.time()
            layout = predict(lproc, lmodel, image, LAYOUT_SCORE)
            seen = Counter(TEXT_LABELS.get(l, l) for s, l, _ in layout if s >= TEXT_SCORE)
            yield log("Layout model → " + (" · ".join(f"{n} {k}" for k, n in seen.most_common()) or "nothing"),
                      page=page_no, stage="check", since=t)
            t = time.time()
            dets, blocks = analyse_page(page, image, tatr, layout, threshold, pdpi)
            ordered = [d for d in dets if "order" in d]
            yield log(f"Reading order (docling ReadingOrderPredictor) → {len(ordered) + len(blocks)} item(s), column by column"
                      + (f" · {sum('title' in d for d in ordered)}/{len(ordered)} table(s) titled" if ordered else ""),
                      page=page_no, stage="check", since=t)  # the time of analyse_page: classifier + reading order
            for d in dets:
                yield detection_log(d, page_no)
            for b in blocks:
                if b.get("figure_class"):
                    chart = b["label"] == "chart"
                    yield log(f"picture #{b['id']} → " + ("chart ✓" if chart else "not a chart")
                              + f" (figure classifier: {b['figure_class'].replace('_', ' ')} {b['figure_score']:.0%})",
                              "ok" if chart else "info", page_no, "check", tag="chart" if chart else "picture")
            scores += boxes
            tables = sum(d["is_table"] for d in dets)
            pages.append((page_no, dets, tables, blocks))
            jpg, image_file = jpeg(image), f"{stem}/pages/p{page_no:03}.jpg"
            (OUT / image_file).write_bytes(jpg)
            results.append({"page": page_no, "width": image.width, "height": image.height, "rotation": page.rotation,
                            "tables": tables, "image_file": image_file, "detections": dets, "text_blocks": blocks})
            yield {"type": "page", **results[-1], "image": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}

        accepted = [d for _, dets, _, _ in pages for d in dets if d["is_table"]]
        texts = Counter(b["label"] for *_, blocks in pages for b in blocks)
        yield log(f"Done · {doc.page_count} pages in {time.time() - t0:.1f} s · {len(accepted)} table(s) on "
                  f"{sum(t > 0 for _, _, t, _ in pages)} page(s) · "
                  f"{sum(not d['is_table'] for _, dets, _, _ in pages for d in dets)} rejected · "
                  f"{texts['paragraph']} paragraph(s) · {texts['chart']} chart(s)", "ok")
        summary = {"total_pages": doc.page_count,
                   "pages_with_tables": sum(t > 0 for _, _, t, _ in pages), "tables": len(accepted),
                   "tables_both_models": sum(d["tatr_score"] is not None for d in accepted),
                   "tables_layout_only": sum(d["tatr_score"] is None for d in accepted),
                   "model_detections": len(scores),
                   "rejected_non_tabular": sum(not d["is_table"] for _, dets, _, _ in pages for d in dets),
                   "paragraphs": texts["paragraph"], "headings": texts["heading"] + texts["title"], "charts": texts["chart"],
                   "text_snippets": sum(texts.values()) - texts["paragraph"] - texts["heading"] - texts["title"],
                   "text_blocks_by_label": dict(texts),
                   "avg_score": round(statistics.mean(scores), 4) if scores else None,
                   "seconds": round(time.time() - t0, 1),
                   "tables_per_page": {str(p): t for p, _, t, _ in pages if t}}
        # same shape as the UI's "Full response" download, so either file can be re-opened in the UI
        out = tables_json(stem)
        out.parent.mkdir(exist_ok=True)
        write_atomic(out, json.dumps({"file": name, "model": MODEL, "layout_model": LAYOUT_MODEL, "threshold": threshold,
                                      "dpi": dpi, "summary": summary, "pages": results}, indent=2, ensure_ascii=False))
        yield log(f"Saved output/{out.relative_to(OUT).as_posix()} + {len(results)} page image(s) in output/{stem}/pages/", "ok")
        yield {"type": "summary", **summary}
    except Exception as e:  # surface failures to the page instead of a silently dead job
        yield log(f"{type(e).__name__}: {e}", "err")
        yield {"type": "error", "detail": f"{type(e).__name__}: {e}"}
    finally:
        doc.close()


def recognise(doc, stem, mode, pages):
    """Recognition job on output/<stem>/table/<stem>_tables.json, read by content.py. Mode "tables" (the UI's
    "Tables + Chart only"): every table and chart. Mode "all" (the UI's "Text + Tables + Chart"): every layout region
    too -- headings, paragraphs, ... with their PDF text-layer text, pictures read from their image -- and the pages' reading
    order. Only scanned tables, charts and (mode "all") scanned regions and pictures go to PaddleOCR-VL (QR codes and
    barcodes are decoded instead; signatures and icons stay placeholders)
    (content.needs_vlm). Events: rmeta, log, regions (a page's text regions at once), table, region (one the VLM
    read), progress (tables + VLM regions), rsummary.
    Speed: the text regions need no model and go out in bulk; the VLM regions go to PaddleOCR-VL content.VLM_SLOTS
    at a time in worker threads while Table Transformer reads the tables; output/<stem>/structure/<stem>_structure.json
    is saved every SAVE_EVERY seconds and at the end (also when the job stops early), not after each item. Entries
    not re-read this time are kept while the detection still has the same box under their (page, id)."""
    try:
        t0 = time.time()
        content.printed_only(doc)  # as at detection: a scan's hidden OCR text is not its text layer
        result = json.loads(tables_json(stem).read_text(encoding="utf-8"))
        want = [p for p in result["pages"] if not pages or p["page"] in pages]
        tabs = [(p["page"], d) for p in want for d in p["detections"] if d["is_table"]]
        charts = [(p["page"], b) for p in want for b in p["text_blocks"] if b["label"] == "chart"]
        texts = [(p["page"], b) for p in want for b in p["text_blocks"] if mode == "all" and b["label"] != "chart"]

        path = structure_json(stem)
        path.parent.mkdir(exist_ok=True)
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        # ids follow the reading order, so a new detection can give an old id to another box: an earlier entry is
        # kept only while its (page, id) still has the same box (within 1 pt: rounding)
        boxes = {(p["page"], "table", d["id"]): content.table_rect(d) for p in result["pages"] for d in p["detections"]
                 if d["is_table"]} | {(p["page"], "region", b["id"]): b["bbox_pdf_pt"] for p in result["pages"]
                                      for b in p["text_blocks"]}
        same = lambda s, k: k in boxes and all(abs(x - y) <= 1 for x, y in zip(boxes[k], s["bbox_pdf_pt"]))
        # pictures an earlier version read with PaddleOCR-VL keep that text, not a placeholder now
        read_before = {k for b in old.get("regions", []) if b["label"] == "picture" and b.get("text")
                       and same(b, k := (b["page"], "region", b["id"]))}
        reads = charts + [(p, b) for p, b in texts if content.needs_vlm(b)]  # to PaddleOCR-VL
        texts = [(p, b) for p, b in texts if not content.needs_vlm(b) and (p, "region", b["id"]) not in read_before]
        total = len(tabs) + len(reads)
        yield {"type": "rmeta", "total": total, "tables": len(tabs), "regions": len(reads) + len(texts), "mode": mode}
        scope = "all pages" if not pages else f"{len(pages)} page(s)"
        yield log(f"Recognition · {'tables + charts' if mode == 'tables' else 'text + tables + charts'} · {scope} · "
                  f"{len(tabs)} table(s) + {len(charts)} chart(s)"
                  + (f" + {len(texts)} text region(s) / picture(s) + {len(reads) - len(charts)} scanned region(s) / "
                     "picture(s)" if mode == "all" else ""), "ok")
        blocks = [b for p in result["pages"] for b in p["text_blocks"]]
        if any(b["label"] == "picture" for b in blocks) and not any("figure_class" in b for b in blocks):
            yield log("This result was detected before chart detection: its pictures are not checked for charts. "
                      "Run the detection again to read them.", "warn")

        t = time.time()
        yield log(f"Loading {content.STRUCTURE_MODEL} + {content.structure_tatr.MODEL} + {content.ORIENTATION_MODEL} …",
                  stage="model")
        content.structure_tatr.load(content.STRUCTURE_MODEL), content.structure_tatr.load(), content.orienter()
        yield log("Recognition models ready", "ok", stage="model", since=t)
        yield log(f"{content.VLM} (llama-server, scans, charts + pictures, {content.VLM_SLOTS} at a time) "
                  + ("answering at " + content.VLM_URL if content.alive(content.VLM_URL)
                     else "not running yet: starts on the first scanned table or chart"), stage="model")

        redo = {(p, "table", d["id"]) for p, d in tabs} | {(p, "region", b["id"]) for p, b in reads + texts}
        tables =[s for s in old.get("tables", []) if (k := (s["page"], "table", s["table_id"])) not in redo and same(s, k)]
        regions = [b for b in old.get("regions", []) if (k := (b["page"], "region", b["id"])) not in redo and same(b, k)]

        def joins():  # tables continued over pages; a failure here must not cost the saved result (reported at the end)
            try:
                return content.join_pages(tables, result["pages"]), None
            except Exception as e:
                return [], f"{type(e).__name__}: {e}"

        def save():
            t = time.time()
            write_atomic(path, json.dumps({"file": result["file"], "tables_json": tables_json(stem).relative_to(OUT).as_posix(),
                                           "model": f"{content.STRUCTURE_MODEL} / {content.VLM}",
                                           "pages": content.reading_order(tables, regions), "tables": tables,
                                           "joined_tables": joins()[0],
                                           "regions": regions}, indent=2, ensure_ascii=False))
            return time.time() - t

        failed, n, saves, save_s, saved = 0, 0, 0, 0.0, time.time()
        pool, futures, pending = ThreadPoolExecutor(content.VLM_SLOTS), {}, []

        def checkpoint():
            nonlocal save_s, saves, saved
            if time.time() - saved >= SAVE_EVERY:
                save_s += save()
                saves, saved = saves + 1, time.time()

        def feed():
            """pending regions rendered and sent to PaddleOCR-VL, 2 x VLM_SLOTS ahead at most (page images are big)."""
            nonlocal failed, n
            while pending and len(futures) < 2 * content.VLM_SLOTS:
                page_no, b = pending.pop(0)
                try:  # rendered here (PyMuPDF is not thread-safe); the worker threads only wait on the VLM
                    page = doc[page_no - 1]
                    futures[pool.submit(content.read_chart, *content.chart_inputs(page, b), b) if b["label"] == "chart"
                            else pool.submit(content.read, content.render(page, b["bbox_pdf_pt"]), b)] = (page_no, b)
                except Exception as e:  # one unrenderable region must not cost the rest
                    failed, n = failed + 1, n + 1
                    yield log(f"{b['label']} #{b['id']} failed: {type(e).__name__}: {e}", "err", page_no, "render")
                    yield {"type": "progress", "n": n, "total": total}

        def charts_done(wait):
            """Events of the regions PaddleOCR-VL has answered (wait: all of them, as they finish); more are sent as
            these come back."""
            while futures:
                ready = [f for f in list(futures) if f.done()] or ([next(as_completed(list(futures)))] if wait else [])
                if not ready:
                    return
                yield from answered(ready)
                yield from feed()
                checkpoint()

        def answered(ready):
            nonlocal failed, n
            for f in ready:
                page_no, item = futures.pop(f)
                try:
                    b = {**item, **f.result(), "page": page_no}  # a chart: checked against its text layer (read_chart)
                    regions.append(b)
                    cut = "" if b["complete"] else " · CUT OFF"
                    if ct := b.get("chart_table"):
                        ck = b["check"]
                        yield log(f"chart #{item['id']} ({b['figure_class']} {b['figure_score']:.0%}) → {b['read_by']} · "
                                  f"{b['crop_dpi']} dpi" + (f" (best of {b['reads']} reads)" if b["reads"] > 1 else "") + " · "
                                  f"chart data table {ct['num_rows']}×{ct['num_cols']} (header: {' | '.join(ct['header'])})"
                                  + cut + {"verified": " · numbers ✓ printed in the chart",
                                           "not checked": " · numbers not checked (no text layer)"}.get(
                                      ck["status"], f" · UNVERIFIED: {', '.join(ck['not_in_pdf'])} not printed in the chart"),
                                  "info" if b["complete"] and ct["num_rows"] and ck["status"] != "unverified" else "warn",
                                  page_no, "vlm", since=time.time() - b["seconds"], tag="chart")
                    else:
                        yield log(f"{b['label']} #{item['id']} → {b['read_by']} · {b['words']} word(s){cut}",
                                  "info" if b["complete"] and b["words"] else "warn", page_no, "vlm",
                                  since=time.time() - b["seconds"], tag="text")
                    yield {"type": "region", **b}
                except Exception as e:
                    failed += 1
                    yield log(f"{item['label']} #{item['id']} failed: {type(e).__name__}: {e}", "err", page_no, "vlm")
                n += 1
                yield {"type": "progress", "n": n, "total": total}

        try:
            if texts:  # no model: the text from the detection, one event per page instead of one per region
                t, by_page = time.time(), {}
                for page_no, b in texts:
                    by_page.setdefault(page_no, []).append({**content.region(None, b), "page": page_no})
                for page_no, items in by_page.items():
                    regions.extend(items)
                    yield {"type": "regions", "page": page_no, "items": items}
                yield log("Text regions from the PDF text layer (no VLM) · " + " · ".join(
                          f"{v} {k}" for k, v in Counter(b["label"] for _, b in texts).most_common()), "ok", since=t, tag="text")
            if reads:
                t = time.time()
                try:
                    content.vlm_server()  # started once here, not by several workers at once
                except Exception as e:  # no VLM: these fail (their earlier results kept), the tables are still read
                    keep = {(p, "region", b["id"]) for p, b in reads}
                    redo -= keep
                    regions += [b for b in old.get("regions", []) if (k := (b["page"], "region", b["id"])) in keep and same(b, k)]
                    failed, n, reads = failed + len(reads), n + len(reads), []
                    yield log(f"charts / scans not read: {type(e).__name__}: {e}", "err", stage="vlm")
                    yield {"type": "progress", "n": n, "total": total}
                pending.extend(reads)
                yield from feed()
                if reads:
                    yield log(f"{len(reads)} chart(s) / scanned region(s) to {content.VLM}, {content.VLM_SLOTS} at a "
                              "time, read while Table Transformer reads the tables", stage="vlm", since=t)
            for page_no, item in tabs:
                t = time.time()
                try:  # one unreadable table must not cost the rest of the document
                    s = content.table(doc[page_no - 1], item)
                    tables.append(s)
                    yield log(f"#{item['id']} table → {s['num_rows']}×{s['num_cols']} · {len(s['cells'])} cells · "
                              f"{s['engine'].split('/')[-1]} ({s['route']}) · orientation {s['orientation']}°"
                              + ("" if s["complete"] else " · CUT OFF"), "ok" if s["complete"] else "warn",
                              page_no, "structure", since=t, tag="table")
                    yield {"type": "table", **s}
                except Exception as e:
                    failed += 1
                    yield log(f"table #{item['id']} failed: {type(e).__name__}: {e}", "err", page_no, "structure")
                n += 1
                yield {"type": "progress", "n": n, "total": total}
                yield from charts_done(wait=False)
                checkpoint()
            yield from charts_done(wait=True)
        finally:  # also when the job stops early (page closed): nothing read so far is lost
            pool.shutdown(wait=False, cancel_futures=True)
            save_s += save()
            saves += 1
        yield log(f"Saved output/{path.relative_to(OUT).as_posix()} {saves}× ({save_s:.1f} s writing it)", "ok")
        joined, err = joins()
        if err:
            yield log(f"joining tables over pages failed: {err}", "err", stage="structure")
        elif joined:
            yield log(f"{len(joined)} table(s) continue over pages → joined: " + " · ".join(
                "+".join(f"p{x['page']} #{x['table_id']}" for x in j["parts"]) for j in joined), "ok", tag="table")
        yield {"type": "joined", "tables": joined}

        done = [s for s in tables if (s["page"], "table", s["table_id"]) in redo]
        summary = {"tables": len(done), "regions": sum((b["page"], "region", b["id"]) in redo for b in regions),
                   "charts": sum((b["page"], "region", b["id"]) in redo and b["label"] == "chart" for b in regions),
                   "charts_unverified": sum((b["page"], "region", b["id"]) in redo
                                            and b.get("check", {}).get("status") == "unverified" for b in regions),
                   "failed": failed, "by_engine": dict(Counter(s["engine"].split("/")[-1] for s in done)),
                   "to_vlm_because": dict(Counter(s["route"] for s in done if s["engine"] == content.VLM)),
                   "seconds": round(time.time() - t0, 1)}
        yield log(f"Recognition done · {summary['tables']} table(s) · {summary['regions']} region(s), "
                  f"{summary['charts']} chart(s) ({summary['charts_unverified']} unverified) · {failed} failed · "
                  f"{summary['seconds']} s · saved output/{path.relative_to(OUT).as_posix()}", "ok" if not failed else "warn")
        yield {"type": "rsummary", **summary}
    except Exception as e:
        yield log(f"{type(e).__name__}: {e}", "err")
        yield {"type": "error", "detail": f"{type(e).__name__}: {e}"}
    finally:
        doc.close()


# Jobs + polling instead of one long streamed response: antivirus web filters (Sophos here) hold a
# browser's HTTP response until it completes, which froze a streamed log until the very end. Short
# poll responses complete at once, so the log stays live behind any buffering scanner or proxy.
JOBS = {}  # job id -> {"events": [...], "base": index of events[0], "done": bool, "seen": last poll time, "lock"}


def work(job_id, job, events):
    with RUN:  # a job sent while another runs waits here
        try:
            for ev in events:
                with job["lock"]:
                    job["events"].append(ev)
                if time.time() - job["seen"] > 60:  # page closed / no polls for a minute: stop burning CPU
                    events.close()
                    JOBS.pop(job_id, None)
                    break
        except Exception as e:  # failed outside the job's own try (its PDF did not open): not a job polled forever
            with job["lock"]:
                job["events"].append({"type": "error", "detail": f"{type(e).__name__}: {e}"})
    job["done"] = True


def locked(make):
    """The events of the job generator make() returns, each step of it run holding PDF (it uses PyMuPDF)."""
    with PDF:
        events = make()
    try:
        while True:
            with PDF:
                ev = next(events, None)
            if ev is None:
                return
            yield ev
    finally:
        with PDF:
            events.close()  # its finally closes the PDF


@app.get("/")
def index():
    return FileResponse(Path(__file__).with_name("index.html"))


@app.get("/health")
def health():
    """For the Docker HEALTHCHECK and a quick look: the app answers, PaddleOCR-VL's state, jobs running / queued."""
    return {"ok": True, "vlm": content.alive(content.VLM_URL), "gpu_server": GPU is not None,
            "jobs": sum(not j["done"] for j in list(JOBS.values()))}


# plain def, not async: FastAPI runs them in its thread pool, so reading the PDF / JSON does not stall /events polls
@app.post("/detect")
def detect(file: UploadFile = File(...), threshold: float = Form(0.5), dpi: int = Form(150)):
    data = upload(file)
    if pdf_info(stream=data, filetype="pdf")[0]:
        raise HTTPException(400, "This PDF is password-protected.")
    threshold, dpi = min(max(threshold, 0.05), 0.99), min(max(dpi, 72), 220)
    stem = safe_stem(file.filename)
    if find_pdf(stem) is None:  # recognition reads the tables from it later
        (DOC / f"{stem}.pdf").write_bytes(data)
    local = lambda: locked(lambda: run(pymupdf.open(stream=data, filetype="pdf"), file.filename, threshold, dpi, len(data)))
    return start(on_gpu("detect", data, file.filename, {}, local, threshold=threshold, dpi=dpi) if GPU else local())


@app.post("/recognise")
def recognise_job(name: str = Form(...), mode: str = Form("tables"), pages: str = Form(""),
                  file: UploadFile | None = File(None)):
    """Recognise the tables of a finished detection (output/<name>/table/<name>_tables.json). The PDF is the one of
    that name in doc/; one that is not there yet can be sent along (else 409, and the UI asks for it) and is saved there."""
    stem = safe_stem(name)
    detected = tables_json(stem)
    if not detected.exists():
        raise HTTPException(404, f"output/{detected.relative_to(OUT).as_posix()} not found: run the detection first.")
    if mode not in ("tables", "all"):
        raise HTTPException(400, "mode is 'tables' or 'all'.")
    total = json.loads(detected.read_text(encoding="utf-8"))["summary"]["total_pages"]
    pdf = find_pdf(stem)
    if pdf is None and file is not None:
        data = upload(file)
        if (n := pdf_info(stream=data, filetype="pdf")[1]) != total:  # checked first: a wrong PDF is not kept
            raise HTTPException(400, f"{file.filename} has {n} pages, the detection result {total}: not the same document.")
        pdf = DOC / f"{stem}.pdf"
        pdf.write_bytes(data)
    if pdf is None:
        raise HTTPException(409, f"{name} is not in doc/: add it there (or choose it here, it is saved there).")
    if (n := pdf_info(filename=str(pdf))[1]) != total:
        raise HTTPException(400, f"doc/{pdf.name} has {n} pages, the detection result {total}: not the same document.")
    try:
        wanted = content.structure.parse_pages(pages) if pages.strip() else None
    except ValueError:
        raise HTTPException(400, "pages: e.g. 12-20 or 5,9,30-31")
    local = lambda: locked(lambda: recognise(pymupdf.open(pdf), stem, mode, wanted))
    if GPU:
        files = {f.relative_to(OUT).as_posix(): f.read_bytes() for f in (detected, structure_json(stem)) if f.exists()}
        return start(with_reference(on_gpu("recognise", pdf.read_bytes(), pdf.name, files, local, mode=mode,
                                           pages=wanted), stem, pdf))
    return start(with_reference(local(), stem, pdf))


def with_reference(events, stem, pdf):
    """A recognition job's events; once it has finished, the LLM reference JSON written from its _structure.json
    (reference.py: output/<stem>/reference/<stem>.reference.json, the medical-report schema with every lab test).
    Written after the job's last event, not at its
    summary: a GPU server job's _structure.json arrives here only then. The summary goes out last, as the UI ends
    the job on it."""
    summary = None
    for ev in events:
        if ev.get("type") == "rsummary":
            summary = ev
        else:
            yield ev
    if summary is None:  # failed or stopped: no finished result to convert
        return
    import reference  # here, not at the top: the GPU server image does not carry it (this computer converts)
    t = time.time()
    try:
        with PDF:
            path = reference.write(stem, pdf)
        yield log(f"Reference JSON saved output/{path.relative_to(OUT).as_posix()}", "ok", stage="structure", since=t)
    except Exception as e:  # the recognition result itself is saved already
        yield log(f"Reference JSON not written: {type(e).__name__}: {e}", "err", stage="structure")
    yield summary


def gpu_server():
    """The GPU server's job runner (gpu_server.py, deployed with the `modal` package), or None when it is not
    deployed, there is no access to it or the package is not installed: then every job runs on this computer."""
    try:
        import modal
        pipeline = modal.Cls.from_name(SERVER, "Pipeline")
        pipeline.hydrate()  # fails here, not in the first job, when the server is not deployed or not reachable
        return pipeline()
    except Exception:
        return None


def on_gpu(kind, pdf, name, files, local, **args):
    """A job on the GPU server: its events as they come, and what it writes (page images, _tables.json /
    _structure.json) saved in output/ here, as a local job saves it. local() makes the same job's events here: used
    when the server cannot take the job (down, no network).
    The job is spawned and its events come through the Modal queue EVENTS, not one long stream: a streamed job was
    cancelled by the first drop of this computer's connection to Modal (StreamTerminatedError: Connection lost).
    Spawned, it runs on there, and the short queue reads here are retried until the connection is back (OUTAGE)."""
    import queue

    import modal
    from grpclib.exceptions import StreamTerminatedError
    net = (OSError, StreamTerminatedError, modal.exception.ConnectionError)  # the connection, not the job
    key = uuid.uuid4().hex  # the job's queue partition
    try:
        events = modal.Queue.from_name(EVENTS, create_if_missing=True)
        # the JSON files gzipped both ways (5-10x smaller, lossless): the transfer, not the GPU, set a job's time
        call = GPU.job.spawn(kind, pdf, name, {p: gzip.compress(d) for p, d in files.items()}, args, key)
    except Exception as e:
        yield log(f"GPU server not reachable ({type(e).__name__}): running on this computer instead", "warn")
        yield from local()
        return
    yield log("Sent to the GPU server · the first job after a pause waits ~1 min for it", stage="model")
    result, done, lost, doc = None, False, None, None
    try:
        while True:
            try:
                try:
                    evs = events.get_many(100, timeout=1 if done else 5, partition=key)
                except queue.Empty:
                    if done:
                        break
                    evs = []
                    try:
                        result, done = call.get(timeout=0), True  # finished: one more read takes its last events
                    except TimeoutError:  # still running
                        pass
                if lost:
                    yield log("Connection to the GPU server back", "ok")
                lost = None
            except net as e:
                if not lost:
                    lost = time.time()
                    yield log(f"Connection to the GPU server lost ({type(e).__name__}: {e}): the job runs on there · "
                              "reconnecting …", "warn")
                elif time.time() - lost > OUTAGE:
                    raise
                time.sleep(3)
                continue
            for ev in evs:
                if ev["type"] == "page":  # sent without its image: rendered here from the same PDF, not downloaded
                    with PDF:
                        doc = doc or pymupdf.open(stream=pdf, filetype="pdf")
                        jpg = jpeg(render(doc[ev["page"] - 1], args["dpi"])[0])
                    inside_out(ev["image_file"]).write_bytes(jpg)
                    ev["image"] = "data:image/jpeg;base64," + base64.b64encode(jpg).decode()
                yield ev
        if result:  # path relative to output/: <stem>/table/... or <stem>/structure/...
            write_atomic(inside_out(result["path"]), gzip.decompress(result["data"]))
    except Exception as e:  # failed there (crashed, out of memory) or no connection for OUTAGE: say so
        yield log(f"GPU server: {type(e).__name__}: {e}", "err")
        yield {"type": "error", "detail": f"GPU server: {type(e).__name__}: {e}"}
    finally:
        if doc:
            with PDF:
                doc.close()
        if not done:  # the page was closed or the job given up: free the GPU
            with contextlib.suppress(Exception):
                call.cancel()


def inside_out(rel):
    """OUT / rel, its folder made; refused when rel (from the GPU server) points outside output/."""
    p = (OUT / rel).resolve()
    if not p.is_relative_to(OUT.resolve()):
        raise ValueError(f"GPU server sent a path outside output/: {rel}")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def start(events):
    """Run an event generator as a background job; the page polls /events/<job>."""
    for jid in [j for j, job in list(JOBS.items()) if job["done"] and time.time() - job["seen"] > 120]:
        JOBS.pop(jid, None)  # finished jobs whose page stopped polling (e.g. it hit an error)
    job_id = uuid.uuid4().hex
    JOBS[job_id] = job = {"events": [], "base": 0, "done": False, "seen": time.time(), "lock": threading.Lock()}
    if RUN.locked():
        job["events"].append(log("Waiting for the running job to finish (one job at a time) …", "warn"))
    threading.Thread(target=work, args=(job_id, job, events), daemon=True).start()
    return {"job": job_id}


@app.get("/events/{job_id}")
def events(job_id: str, since: int = 0):
    """Events from index `since` on. Everything before `since` is acknowledged and freed (page images are big)."""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job.")
    with job["lock"]:
        job["seen"] = time.time()
        since = min(since, job["base"] + len(job["events"]))  # never past what was sent: those are not delivered yet
        del job["events"][:max(0, since - job["base"])]
        job["base"] = max(job["base"], since)
        out, done = list(job["events"]), job["done"]
    if done and not out:
        JOBS.pop(job_id, None)
    return {"events": out, "next": job["base"] + len(out), "done": done}


def selfcheck():
    """Fusion: agreement raises confidence, a weak Table Transformer box is rescued by a layout table,
    a layout-only table below threshold is dropped, a nested duplicate is dropped, a table of contents is a table, and a confident
    Table Transformer box on a region the layout model reads as text is rejected with that reason."""
    tatr = [(0.98, "table", [0, 0, 100, 100]), (0.95, "table", [200, 0, 300, 100]), (0.2, "table", [0, 200, 100, 300])]
    layout = [(0.9, "table", [2, 2, 100, 104]), (0.97, "text", [200, 0, 300, 100]), (0.6, "table", [0, 198, 100, 300]),
              (0.55, "table", [0, 400, 100, 500]), (0.5, "table", [5, 5, 50, 50]), (0.7, "document_index", [0, 600, 100, 700])]
    tables, rejected = fuse(tatr, layout, 0.6)
    assert [(round(k["score"], 3), k["layout_label"]) for k in tables] == [(0.998, "table"), (0.7, "document_index"),
                                                                          (0.68, "table")], tables
    assert [(r["score"], r["seen_as"]) for r in rejected] == [(0.95, "text")], rejected
    assert [safe_stem(n) for n in ("../../a b.pdf", "..\\x.pdf", None)] == ["a_b", "x", "document"]
    assert [safe_stem(n) for n in ("report.jpg", "scan 1.PNG", "report_jpg.pdf")] == ["report_jpg", "scan_1_PNG", "report_jpg"]
    # an uploaded photo becomes a one-page PDF: upright by its EXIF orientation (stored sideways, shown portrait),
    # A4 wide when it carries no DPI; a PDF passes as it is; a broken image is a 400
    photo = Image.new("RGB", (400, 300), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: rotate 90 degrees to show it
    buf = io.BytesIO()
    photo.save(buf, "JPEG", exif=exif)
    with pymupdf.open(stream=as_pdf(buf.getvalue(), "phone.jpg"), filetype="pdf") as doc:
        r = doc[0].rect
        assert doc.page_count == 1 and r.height > r.width and abs(r.width - IMAGE_WIDTH_IN * 72) < 1, r
        assert not doc[0].get_text().strip() and len(doc[0].get_images()) == 1  # a scan: no text layer
    png = io.BytesIO()
    Image.new("RGBA", (850, 1100), (255, 255, 255, 0)).save(png, "PNG", dpi=(100, 100))
    with pymupdf.open(stream=as_pdf(png.getvalue(), "scan.png"), filetype="pdf") as doc:
        assert [round(v) for v in (doc[0].rect.width, doc[0].rect.height)] == [612, 792]  # 8.5 x 11 in at 100 dpi
    assert as_pdf(b"%PDF-1.7 ...", "report.pdf") == b"%PDF-1.7 ..."
    try:
        as_pdf(b"not an image", "x.png")
        raise AssertionError("a broken image passed")
    except HTTPException as e:
        assert e.status_code == 400
    # text_grid(): a borderless lab table (header, a row whose range runs on in its own column, a last row) is a
    # grid; a signature block (aligned, no figures) and a form of label / value pairs (two columns) are not
    w = lambda x0, x1, y, s: (x0, y, x1, y + 9, s)
    lab = [w(53, 75, 250, "Test"), w(253, 283, 250, "Result"), w(351, 370, 250, "Unit"), w(423, 530, 250, "Biological"),
           w(54, 85, 272, "HbA1c"), w(241, 247, 272, "H"), w(250, 272, 272, "7.10"), w(351, 360, 272, "%"),
           w(423, 493, 272, "Screening:"), w(423, 468, 284, "Diabetes:"), w(471, 500, 284, ">6.5%"),
           w(53, 146, 380, "Mean Blood Glucose"), w(253, 283, 380, "157.07"), w(351, 379, 380, "mg/dL")]
    assert text_grid(lab)
    signatures = [w(48, 147, 730, "Dr. Tejaswini"), w(243, 319, 730, "Dr. Sanjeev"), w(429, 487, 730, "Dr. Yash"),
                  w(48, 117, 746, "M.D. Pathology"), w(243, 286, 746, "MD Path"), w(429, 464, 746, "MD Path"),
                  w(48, 117, 760, "Reg no 2012"), w(243, 286, 760, "Reg no 3093"), w(429, 464, 760, "Reg no 4471")]
    assert not text_grid(signatures)
    form = [w(40, 100, 100 + 14 * i, label) for i, label in enumerate(("Name", "Age", "Visit", "Date"))] + \
           [w(140, 220, 100 + 14 * i, value) for i, value in enumerate(("Jane", "47", "12", "16-09-2025"))]
    assert not text_grid(form)
    banner = [w(64, 100, 120, "NAME"), w(135, 300, 120, ": MR X(51Y/M)"), w(366, 460, 120, "HOME COLLECTION :"),
              w(64, 100, 134, "REF. BY"), w(135, 200, 134, ": SELF"), w(366, 470, 134, "HOSPITAL 12"),
              w(64, 120, 148, "TEST ASKED"), w(135, 260, 148, ": PACKAGE 3"), w(366, 520, 148, "ROAD 401107")]
    assert not text_grid(banner)
    colons = [w(389, 420, 132 + 16 * i, label) for i, label in enumerate(("Regn Date", "Collected", "Reported"))] + \
             [w(458, 530, 132 + 16 * i, f"16-Sep-2025 1{i}:00") for i in range(3)] + \
             [w(451, 453, 135 + 16 * i, ":") for i in range(3)] + [w(254, 330, 148, "SERVICE LIMITED")]
    assert not text_grid(colons)  # the colons a line of their own, 3 points below the labels


if __name__ == "__main__":
    selfcheck()
    content.selfcheck()
    import medical
    import reference
    medical.selfcheck()
    reference.selfcheck()
    uvicorn.run(app, host="127.0.0.1", port=8000)
