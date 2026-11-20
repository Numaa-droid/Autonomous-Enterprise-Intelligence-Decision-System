"""What is inside the tables and pictures app.py found: table structure + cell text, picture / scan content.

  * tables: Table Transformer v1.1-all (structure_tatr.py) reads the grid -- bordered or borderless, also with
    sideways header labels; spanning cells and header fused over several crops -- and each PDF word (PyMuPDF)
    goes to the cell it overlaps most. (It replaced TableFormerV2, which merged neighbouring columns.) A table
    printed sideways is turned upright first (structure_tatr.upright: the PDF's writing direction); when it is
    printed with ruling lines, its grid comes from those lines instead (structure_tatr.ruled: dense multi-level
    sideways tables break Table Transformer, adjacency F1 45 -> 84 %). A table with no text layer (a scanned page)
    goes to PaddleOCR-VL-1.6 at 216 dpi, turned upright by PP-LCNet_x1_0_doc_ori (PaddleOCR's orientation classifier).
  * charts (bar / histogram, line, pie, scatter, box plot -- the detection labels them "chart" with docling's
    DocumentFigureClassifier): PaddleOCR-VL-1.6 "Chart Recognition:" gives their data, kept as a table in chart_table.
  * every other layout region (heading, paragraph, list item, caption, ...) keeps the text the PDF's text layer
    gave it at detection; one with no text layer (a scan) is read by PaddleOCR-VL-1.6 ("OCR:"), a picture the figure
    classifier calls a table too ("Table Recognition:"); other pictures are kept as placeholders, not read.
  * reading order (page_order, at detection): docling's ReadingOrderPredictor (docling-ibm-models) over the page's
    tables and layout regions -- page header, then the body column by column, then page footer. table_titles links
    each table to the heading right before it in that order (its title) and a short line in between (subtitle).
  * clean: the invisible characters a PDF text layer carries (control / format / private-use: a BEL before a
    heading, symbol-font bullets) are dropped from every text, as they show as boxes; the joiners ZWJ / ZWNJ stay.
PaddleOCR-VL-1.6 is the GGUF build served by llama-server (OpenAI API at VLM_URL); started here on first use when
nothing answers there.
"""

import base64
import functools
import hashlib
import io
import json
import os
import re
import shutil
import math
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np
import pymupdf
import torch
from docling_core.types.doc.base import CoordOrigin, Size
from docling_core.types.doc.labels import DocItemLabel
from docling_ibm_models.reading_order.reading_order_rb import PageElement, ReadingOrderPredictor
from huggingface_hub import hf_hub_download
from PIL import Image
from transformers import AutoImageProcessor, AutoModelForImageClassification

import structure
import structure_tatr

# v1.1-all fine-tuned on Azure Document Intelligence table labels; on the held-out test reports: rows right 82.7 %, columns
# right 82.7 %, cell adjacency F1 94.0 %, perfect tables 75.3 %. structure_tatr.MODEL is the original: a table goes
# to it only when its views agree clearly more on the grid (structure_tatr.steadiest, MARGIN; the fine-tuned model
# is unsure on some tables). The previous fine-tuned model is in models/tatr-finetuned-model2.
STRUCTURE_MODEL = str(Path(__file__).parent / "models" / "tatr-finetuned-01-11")
# PaddleOCR-VL (scanned tables, charts) is llama-server: on the GPU server it runs beside the models; on this
# computer it is started on first use when installed (winget install llama.cpp)
VLM = "PaddleOCR-VL-1.6"
VLM_REPO = "PaddlePaddle/PaddleOCR-VL-1.6-GGUF"
VLM_URL = os.environ.get("VLM_URL", "http://127.0.0.1:8080")
# requests llama-server answers at once (its --parallel slots); app.recognise sends the charts this many at a time
VLM_SLOTS = int(os.environ.get("VLM_SLOTS", "4"))
SLOT_CONTEXT = 16384  # tokens per slot: a page image + up to 8192 answer tokens (vlm max_tokens)
VLM_TIMEOUT = 600  # seconds per request: 8192 tokens on a slow CPU fit; a hung request frees its worker after this
VLM_LOG = structure.OUT / "llama-server.log"  # llama-server's own output, when this process started it
VLM_START = threading.Lock()
ORIENTATION_MODEL = "PP-LCNet_x1_0_doc_ori"
# a scanned table goes to PaddleOCR-VL at 216 dpi, as charts do: small print in a scan is a few pixels high at 144 dpi
TABLE_SCALE = 3.0
FIGURE_MODEL = "docling-project/DocumentFigureClassifier-v2.5"
# the DocumentFigureClassifier classes that make a picture a "chart" region (a histogram is a bar_chart to the classifier); its data comes
# back from PaddleOCR-VL as a table (chart_table)
CHARTS = {"bar_chart", "line_chart", "pie_chart", "scatter_plot", "box_plot"}
# pictures: every one is read (read) but these, which hold nothing to read (handwriting, a symbol); QR codes and
# barcodes are decoded (codes), a picture of a table gets "Table Recognition:", any other "OCR:" (a logo's or a
# stamp's text, a screenshot, a scanned page pasted in as an image)
UNREAD = {"signature", "icon"}
CODES = {"qr_code", "bar_code"}
VLM_SEEN, VLM_SEEN_LOCK, VLM_SEEN_MAX = {}, threading.Lock(), 1024  # vlm(): answers by (task, image), oldest out first
# a chart is read from a sharper crop than a table (small value labels: a few pixels each at 144 dpi), and once more
# at the next scale when its numbers are not all printed in the chart (read_chart); the better read is kept
CHART_SCALES = (3.0, 2.0)  # 216 dpi, then 144 dpi
# points added around a chart box: the layout model frames the drawing, and labels printed beside it were cut off
CHART_PAD = 12
# table_titles: the layout labels a table title can have, and the most words of a subtitle line between it and the table
TITLE_LABELS = {"title", "section_header", "caption"}
SUBTITLE_WORDS = 15
OTSL = re.compile(r"<(fcel|ecel|lcel|ucel|xcel|nl|ched|rhed|srow)>")


# ---------- models (each loads on first use) ----------

@functools.cache
def orienter():
    from paddleocr import DocImgOrientationClassification  # paddle is imported only once a table is read
    return DocImgOrientationClassification(model_name=ORIENTATION_MODEL)


@functools.cache
def figure_model():
    return AutoImageProcessor.from_pretrained(FIGURE_MODEL), AutoModelForImageClassification.from_pretrained(FIGURE_MODEL).eval()


def angles(images):
    """Orientation of each image: 0 / 90 / 180 / 270 degrees, as turning it counter-clockwise by that sets it upright."""
    if not images:
        return []
    arrays = [np.ascontiguousarray(np.asarray(i.convert("RGB"))[:, :, ::-1]) for i in images]  # BGR, as paddlex reads
    return [int(r["label_names"][0]) for r in orienter().predict(arrays)]


def figure_classes(images):
    """[(class, score)] of pictures, one batch on the model's device (the GPU on the GPU server): bar_chart,
    pie_chart, table, photograph, logo, ..."""
    if not images:
        return []
    proc, model = figure_model()
    with torch.inference_mode():
        p = model(**proc(images=[i.convert("RGB") for i in images], return_tensors="pt").to(model.device)).logits.softmax(-1)
    return [(model.config.id2label[int(r.argmax())], float(r.max())) for r in p]


def alive(url):
    try:
        with urllib.request.urlopen(url + "/health", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def vlm_server():
    """VLM_URL, after starting llama-server with PaddleOCR-VL-1.6 there if nothing answers (one start at a time:
    the worker threads ask for it at once)."""
    with VLM_START:
        return start_vlm()


@functools.cache
def start_vlm():
    if alive(VLM_URL):
        return VLM_URL
    exe = shutil.which("llama-server")
    if not exe:
        raise RuntimeError(f"No llama-server at {VLM_URL} and none on PATH to start (winget install llama.cpp)")
    model = hf_hub_download(VLM_REPO, "PaddleOCR-VL-1.6-GGUF.gguf")
    mmproj = hf_hub_download(VLM_REPO, "PaddleOCR-VL-1.6-GGUF-mmproj.gguf")
    u = urlparse(VLM_URL)
    proc = subprocess.Popen([exe, "-m", model, "--mmproj", mmproj, "--host", u.hostname, "--port", str(u.port or 80),
                             "--temp", "0", "-ngl", "99",  # every layer on the GPU when there is one; a CPU-only build ignores it
                             "-np", str(VLM_SLOTS), "-c", str(VLM_SLOTS * SLOT_CONTEXT)],  # VLM_SLOTS requests at once
                            stdout=VLM_LOG.open("ab"), stderr=subprocess.STDOUT)  # why it died, when it does
    for _ in range(300):
        if alive(VLM_URL):
            return VLM_URL
        time.sleep(1)
    proc.kill()  # not left holding the port: the next job starts a fresh one
    raise RuntimeError(f"llama-server did not come up at {VLM_URL} within 5 minutes")


def vlm(image, task):
    """(text, complete) of PaddleOCR-VL-1.6 for one image and task prompt; complete is False when it hit max_tokens.
    The same image and task again (a lab's logo or stamp on every page) is answered from VLM_SEEN, not read again."""
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "PNG")
    key = (task, hashlib.sha1(buf.getvalue()).hexdigest())
    with VLM_SEEN_LOCK:
        if key in VLM_SEEN:
            return VLM_SEEN[key]
    answer = ask_vlm(buf, task)
    with VLM_SEEN_LOCK:
        if len(VLM_SEEN) >= VLM_SEEN_MAX:
            VLM_SEEN.pop(next(iter(VLM_SEEN)))
        VLM_SEEN[key] = answer
    return answer


def ask_vlm(buf, task):
    """(text, complete) of PaddleOCR-VL-1.6 for one PNG (buf) and task prompt."""
    body = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}},
        {"type": "text", "text": task}]}], "temperature": 0, "max_tokens": 8192}
    post = lambda: urllib.request.urlopen(urllib.request.Request(vlm_server() + "/v1/chat/completions",
                                          json.dumps(body).encode(), {"Content-Type": "application/json"}),
                                          timeout=VLM_TIMEOUT)
    try:
        r = post()
    except urllib.error.URLError as e:  # llama-server died since it started (refused): start it again, once
        if not isinstance(e.reason, ConnectionRefusedError):
            raise
        start_vlm.cache_clear()
        r = post()
    with r:
        choice = json.load(r)["choices"][0]
    return choice["message"]["content"].strip(), choice.get("finish_reason") != "length"


# ---------- PaddleOCR-VL table markup (OTSL with the cell text inline) ----------

def otsl(text):
    """'<fcel>a<lcel><nl>...' -> (tags, the text after each cell tag, in order)."""
    parts = OTSL.split(text)  # [before, tag, text, tag, text, ...]
    tags = parts[1::2]
    return tags, [s.strip() for t, s in zip(tags, parts[2::2]) if t in structure.CELL_TAGS]


def otsl_rows(text):
    """Table markup -> plain rows, ' | ' between cells (merged positions empty); other text is returned as is."""
    if not OTSL.search(text):
        return text
    parts, rows, row = OTSL.split(text), [], []
    for tag, s in zip(parts[1::2], parts[2::2]):
        if tag == "nl":
            rows.append(" | ".join(row))
            row = []
        else:
            row.append(s.strip())
    return "\n".join(rows + ([" | ".join(row)] if row else []))


def chart_table(text):
    """PaddleOCR-VL's chart data ('x | y' lines, the header line first; PaddleX reads it the same way) ->
    {num_rows, num_cols, header, rows}; short rows padded with "", markdown '---|---' lines dropped."""
    lines = [s.strip().strip("|") for s in text.splitlines() if s.strip()]
    rows = [[c.strip() for c in s.split("|")] for s in lines if s and not ("---" in s and set(s) <= set("-:| "))]
    ncols = max(map(len, rows), default=0)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    return {"num_rows": len(rows), "num_cols": ncols, "header": rows[0] if rows else [], "rows": rows[1:]}


# a number as printed: groups of 3 digits after a space / comma / dot ("3 402", "2,151") belong to it
NUMBER = re.compile(r"\d{1,3}(?:[   ,.]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")


def values(number):
    """What a printed number can mean, either decimal comma or dot: "3 402" {3402}, "17,8" {17.8} (not 178),
    "1.234,5" {1234.5}, "2,151" {2151, 2.151} (one separator before 3 digits: thousands or decimals)."""
    s = re.sub(r"[   ]", "", number)
    seps = re.findall(r"[.,]", s)
    if not seps:
        return {float(s)}
    if len(set(seps)) == 2:  # both: the last one is the decimal separator
        return {float(s.replace(seps[0], "").replace(seps[-1], "."))}
    if len(seps) > 1:  # one kind, several times: thousands
        return {float(s.replace(seps[0], ""))}
    whole, tail = s.split(seps[0])
    return {float(whole + tail), float(f"{whole}.{tail}")} if len(tail) == 3 else {float(f"{whole}.{tail}")}


def unprinted(chart, printed):
    """The numbers of a chart_table not printed in the text printed (compared by value): each printed number is used
    once, so a value the VLM repeats is caught too."""
    left = [values(m) for m in NUMBER.findall(printed)]
    extra = []
    for cell in [c for row in [chart["header"], *chart["rows"]] for c in row]:
        for m in NUMBER.findall(cell):
            hit = next((i for i, v in enumerate(left) if v & values(m)), None)
            if hit is None:
                extra.append(m)
            else:
                left.pop(hit)
    return extra


def check(chart, printed):
    """PaddleOCR-VL's chart data against the chart's PDF text layer (printed): every number it gives must be printed
    in the chart -> {"status": verified | unverified | not checked, "not_in_pdf": [numbers]}. "not checked": the text
    layer there has no number (an image of a chart): only the VLM can read it. A right number under the wrong label is
    not caught."""
    if not NUMBER.search(printed):
        return {"status": "not checked", "not_in_pdf": []}
    extra = unprinted(chart, printed)
    return {"status": "unverified" if extra else "verified", "not_in_pdf": extra}


def chart_inputs(page, block):
    """(crops of the chart at CHART_SCALES, its PDF text-layer text), both of its box + CHART_PAD. PyMuPDF: called on
    the job's thread; read_chart then needs no PyMuPDF and runs in a VLM worker thread."""
    rect = (pymupdf.Rect(block["bbox_pdf_pt"]) + (-CHART_PAD, -CHART_PAD, CHART_PAD, CHART_PAD)) & page.rect
    return [render(page, rect, s) for s in CHART_SCALES], clean(page.get_text("text", clip=rect))


def read_chart(images, printed, block):
    """read() of a chart from its crops in turn until one gives only numbers printed in the chart (check); the best
    read is kept: complete first, then the fewest numbers not printed. A greedy read of the same image gives the same
    answer, so the next try is another scale. Also "check", "crop_dpi" and "reads" (how many were made)."""
    best, seconds = None, 0.0
    for n, (image, scale) in enumerate(zip(images, CHART_SCALES), 1):
        r = read(image, block)
        seconds += r["seconds"]
        r.update(check=check(r["chart_table"], printed), crop_dpi=round(72 * scale), reads=n)
        if best is None or (not r["complete"], len(r["check"]["not_in_pdf"])) < (not best["complete"],
                                                                                  len(best["check"]["not_in_pdf"])):
            best = r
        if best["complete"] and best["check"]["status"] != "unverified":
            break
    return {**best, "reads": n, "seconds": round(seconds, 2)}


# ---------- tables and regions ----------

def render(page, rect, scale=structure.SCALE):
    """rect in the unrotated frame (as the PDF words); get_pixmap takes its clip in the displayed one (/Rotate pages)."""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=pymupdf.Rect(rect) * page.rotation_matrix)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def table_rect(det):
    """A detected table's PDF rect in whole points, rounded outward (no character at its edges cut off by the clip);
    its _structure.json entry keeps it (app.recognise matches entries by it)."""
    x0, y0, x1, y1 = det["bbox_pdf_pt"]
    return [math.floor(x0), math.floor(y0), math.ceil(x1), math.ceil(y1)]


def table(page, det):
    """One table app.py found -> its structure in the _structure.json format (cells with row / col / spans /
    header flags / text), read by Table Transformer v1.1-all + the text layer, or by PaddleOCR-VL-1.6 (`route`
    says why)."""
    t0, rect = time.time(), table_rect(det)
    words = [(*w[:4], s, *w[5:]) for w in page.get_text("words", clip=rect) if (s := clean(w[4]))]
    # order / title / subtitle: from the detection (page_order, table_titles), when it gave them
    out = {"page": page.number + 1, "table_id": det["id"], "bbox_pdf_pt": rect,
           **{k: det[k] for k in ("order", "title", "subtitle") if k in det}}
    if words:  # sideways or not: the grid from the upright table, the text from the PDF
        turn = structure_tatr.upright(page, rect)
        objs, model, agree = structure_tatr.steadiest([structure_tatr.load(STRUCTURE_MODEL),
                                                       structure_tatr.load(structure_tatr.MODEL)], page, rect, turn)
        out["agreement"] = round(agree, 3)
        up = [(*structure_tatr.frame(w, turn), *w[4:]) for w in words]
        engine = Path(model.name_or_path).name
        route = "Table Transformer v1.1-all grid + PDF text layer" + (" · turned upright" if turn else "")
        lined = structure_tatr.ruled(page, rect, turn, up) if turn else None
        if lined:  # a sideways table printed with ruling lines: its grid from them; header rows still Table Transformer's
            objs = lined + [o for o in objs if o[0] in ("table column header", "table projected row header")]
            engine, route = "PDF ruling lines", "grid from the PDF's ruling lines + PDF text layer · turned upright"
        sized = [(*u[:8], *s[8:]) for u, s in zip(up, structure_tatr.sized(page, words))]  # upright, with font sizes
        if not lined:  # rows checked against the text layer: two printed rows read as one are cut apart
            objs = structure_tatr.text_rows(objs, sized)
        cells, nrows, ncols = structure_tatr.build_cells(objs)
        if not lined:  # a text running across a column line drawn through it joins those cells
            cells = structure_tatr.text_spans(cells, sized)
        structure_tatr.fill(cells, up)
        for c in cells:
            c["bbox_pdf_pt"] = [round(v, 2) for v in structure_tatr.frame(c["bbox_pdf_pt"], turn, back=True)]
        return {**out, "orientation": (360 - turn) % 360, "engine": engine, "route": route,
                "seconds": round(time.time() - t0, 2), "complete": True, "num_rows": nrows, "num_cols": ncols,
                "cells": cells}
    crop = render(page, rect, TABLE_SCALE)
    turn = angles([crop])[0]
    return {**out, "orientation": turn, "engine": VLM, **by_vlm(crop.rotate(turn, expand=True), rect),
            "route": "no text layer", "crop_dpi": round(72 * TABLE_SCALE), "seconds": round(time.time() - t0, 2)}


def by_vlm(image, rect):
    """PaddleOCR-VL-1.6 reads an upright table image -> complete, num_rows, num_cols, otsl, cells (text from the
    image, no cell boxes: the VLM writes text, not positions)."""
    text, complete = vlm(image, "Table Recognition:")
    return grid(*otsl(text), rect, complete)


def grid(tags, texts, rect, complete=True):
    cells, nrows, ncols = structure.build_cells(tags, [], rect)
    for c, s in zip(cells, texts):
        c["text"] = s
    spread(cells)
    return {"complete": complete, "num_rows": nrows, "num_cols": ncols, "otsl": " ".join(tags), "cells": cells}


AMOUNT = re.compile(r"[-–(]?\d[\d.,]*\)?%?")  # one printed amount, whole: 1.234,56 · -868.699,33 · (1.234) · 5,7%
THOUSANDS = re.compile(r"-?\d{1,3}( \d{3})+([.,]\d+)?")  # "3 402": ONE number with a thousands space


def spread(cells):
    """Every table PaddleOCR-VL reads: when the image has more number columns than column headings (e.g. a subtotal
    column between two period columns), the VLM writes a row's numbers into one cell and leaves the cells before it
    empty ("label", "", "1.234,56 7.890,12"). Such numbers go one per cell into the empty cells before it, left to
    right. Only a cell of nothing but numbers, and only into empty cells after the first column (the labels)."""
    at = {(c["row"], c["col"]): c for c in cells if c["row_span"] == c["col_span"] == 1}
    for c in list(at.values()):
        text = (c.get("text") or "").strip()
        nums = text.split()
        if len(nums) < 2 or not all(AMOUNT.fullmatch(x) for x in nums) or THOUSANDS.fullmatch(text):
            continue
        left = [at.get((c["row"], c["col"] - k)) for k in range(len(nums) - 1, 0, -1)]
        if c["col"] - len(nums) + 1 >= 1 and all(x is not None and not (x.get("text") or "").strip() for x in left):
            for x, s in zip(left + [c], nums):
                x["text"] = s


def printed_only(doc):
    """doc's text layer without the text nobody sees: text drawn invisible (render mode 3), which a scanner's or an
    OCR tool's "searchable PDF" lays over the page image. That text is the tool's guess at the image -- often a poor
    one (a Canon scan: "BAL^NCEs", "t999" for 1999) -- and with it a scanned page passes for a born-digital one: its
    tables went to Table Transformer with that text, its regions kept it, and nothing went to PaddleOCR-VL. Without
    it a scan is what it shows, an image, and is read from the image like any scan; on a page that also prints text
    (a born-digital page with a scanned image pasted in, a stamp added later) that text stays, as do drawings and
    images. In memory only, the PDF file is not changed. -> the numbers of the pages that had such text."""
    pages = []
    for page in doc:
        if not any(span["type"] == 3 for span in page.get_texttrace()):
            continue
        try:
            page.add_redact_annot(page.rect, fill=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE_INVISIBLE)
            pages.append(page.number + 1)
        except Exception:  # a page PyMuPDF cannot rewrite keeps its text layer, as before
            continue
    return pages


def needs_vlm(block):
    """A chart, a picture with something to read in it (any but a signature or an icon), or a text region with
    nothing in the PDF text layer (a scan): these are read from their image (read)."""
    if block["label"] == "picture":
        return block.get("figure_class") not in UNREAD
    return block["label"] == "chart" or not block["text"]


def codes(image):
    """The texts of the QR codes and barcodes in an image (OpenCV), [] when none decodes. Tried as rendered, then
    twice as large; with a white margin (the quiet zone a decoder needs, often cropped off by the picture's box)."""
    grey = np.asarray(image.convert("L"))
    for k in (1, 2):
        im = grey if k == 1 else cv2.resize(grey, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
        im = cv2.copyMakeBorder(im, 32, 32, 32, 32, cv2.BORDER_CONSTANT, value=255)
        ok, qr, *_ = cv2.QRCodeDetector().detectAndDecodeMulti(im)
        ok_b, bars, *_ = cv2.barcode.BarcodeDetector().detectAndDecodeWithType(im)
        found = [s for s in (list(qr) if ok else []) + (list(bars) if ok_b else []) if s]
        if found:
            return list(dict.fromkeys(found))
    return []


def region(page, block):
    """One layout region. needs_vlm: read from its image (read). A signature or an icon: a placeholder, not read.
    Anything else keeps its PDF text-layer text (no VLM)."""
    if needs_vlm(block):
        return {**block, **read(render(page, block["bbox_pdf_pt"]), block)}
    if block["label"] == "picture":
        return {**block, "text": "", "words": 0, "read_by": "not read (picture)", "complete": True, "seconds": 0.0}
    return {**block, "read_by": "PDF text layer", "complete": True, "seconds": 0.0}


def read(image, block):
    """A needs_vlm region's image -> PaddleOCR-VL-1.6's text: text, words, read_by, complete, seconds; a chart
    ("Chart Recognition:") also its data as a table (chart_table), a picture of a table "Table Recognition:" as
    ' | ' rows, anything else "OCR:". Only the VLM call, no PyMuPDF (not thread-safe): app.recognise runs it in
    worker threads."""
    t0 = time.time()
    figure = block.get("figure_class") if block["label"] == "picture" else None
    if figure in CODES:  # a QR code / barcode: decoded, exactly; a barcode not decoded still has its digits printed
        found = codes(image)
        if found or figure == "qr_code":
            text = "\n".join(found)
            return {"text": text, "words": len(text.split()), "codes": found, "complete": True,
                    "read_by": "OpenCV code decoder" + ("" if found else " · nothing decodable"),
                    "seconds": round(time.time() - t0, 2)}
    task = ("Chart Recognition:" if block["label"] == "chart" else "Table Recognition:" if figure == "table"
            else "OCR:")
    text, complete = vlm(image, task)
    if not text.strip() and block["label"] == "picture":
        text = block.get("text") or ""  # nothing to read in the image: the PDF text printed over it, if any
    text = otsl_rows(text)
    return {"text": text, "words": len(text.split()), "read_by": f"{VLM} · {task}", "complete": complete,
            **({"chart_table": chart_table(text)} if block["label"] == "chart" else {}),
            "seconds": round(time.time() - t0, 2)}


def clean(text):
    """text without control, format and private-use characters (Unicode category C*: a BEL before a heading, symbol-
    font bullets, zero-width spaces), which a PDF text layer carries and which show as boxes; whitespace and the
    joiners ZWJ / ZWNJ (they shape Devanagari conjuncts) are kept. Without filler either: FILLER or more of one
    separator character ("-----", "_____", ".....", "=====") is a line typed in text, a rule or a dot leader, not
    content; a "-" or "--" (nil) or a "-0.0-" stays."""
    text = "".join(ch for ch in text if ch.isspace() or ch in "‌‍" or unicodedata.category(ch)[0] != "C")
    return FILLER_RE.sub("", text)


FILLER = 4  # this many of one separator character in a row is filler (clean())
FILLER_RE = re.compile(rf"([-_=.·*~–—‒―─━•…])\1{{{FILLER - 1},}}")


def page_order(size, elements):
    """Indices of one page's elements in reading order, by docling's ReadingOrderPredictor: page headers, then the
    body column by column (top to bottom in each), then page footers. size: (width, height); elements: [(label,
    [x0, y0, x1, y1])], top-left origin, label a layout-model / docling label (text, section_header, table, chart, ...)."""
    w, h = size
    # bottom-left origin: the predictor's own conversion from top-left drops the element fields
    elems = [PageElement(cid=i, page_no=0, page_size=Size(width=w, height=h),
                         label=DocItemLabel._value2member_map_.get(label, DocItemLabel.TEXT),
                         l=b[0], t=h - b[1], r=b[2], b=h - b[3], coord_origin=CoordOrigin.BOTTOMLEFT)
             for i, (label, b) in enumerate(elements)]
    out = [e.cid for e in ReadingOrderPredictor().predict_reading_order(elems)]
    return out + [i for i in range(len(elements)) if i not in set(out)]  # never lose a region


def table_titles(elements):
    """{index of a table: (title index, subtitle index or None)} over one page's elements in reading order, [(label,
    [x0, y0, x1, y1], words)]. A table's title is the heading (TITLE_LABELS) right before it; one short text line in
    between (<= SUBTITLE_WORDS words, e.g. "As at 31 March 2025") is its subtitle. Both start above the table and
    share some of its width: the same column."""
    def over(i, t):
        a, b = elements[i][1], elements[t][1]
        return a[1] < b[1] and min(a[2], b[2]) > max(a[0], b[0])

    out = {}
    for t, (label, _, _) in enumerate(elements):
        if label != "table":
            continue
        i, sub = t - 1, None
        if i >= 0 and elements[i][0] == "text" and elements[i][2] <= SUBTITLE_WORDS and over(i, t):
            i, sub = i - 1, i
        if i >= 0 and elements[i][0] in TITLE_LABELS and over(i, t):
            out[t] = (i, sub)
    return out


def reading_order(tables, regions):
    """[{page, items: [[kind, id], ...]}]: every recognised table and region of a page in reading order (the "order"
    page_order gave it at detection; top to bottom for results detected before that); kind is "table" or the
    region's label (heading, paragraph, chart, ...)."""
    key = lambda x: (x["page"], x.get("order", float("inf")), x["bbox_pdf_pt"][1], x["bbox_pdf_pt"][0])
    items = sorted([(key(t), "table", t["table_id"]) for t in tables] + [(key(b), b["label"], b["id"]) for b in regions])
    pages = {}
    for k, kind, i in items:
        pages.setdefault(k[0], []).append([kind, i])
    return [{"page": p, "items": v} for p, v in pages.items()]


RUNNING = {"page header", "page footer"}  # a page's running header / footer: around a table, not next to it
COL_TOL = 10  # points: a table continued on the next page keeps its columns; their edges may move this much


def join_pages(tables, pages):
    """[{parts, title, subtitle, num_rows, num_cols, cells}]: the tables printed over two or more pages, each as
    one table. A table continues on the next page when it is the last thing on its page (only the running header /
    footer after it) and the next page's first table comes before anything else -- text printed above the table on
    the earlier page, a repeated running header, does not count -- with the same columns (orientation 0, the same
    number, each right edge and the left edge within COL_TOL) and
      * no header row (its rows are appended), or
      * the same header rows (reprinted: dropped), or
      * a header continuing the earlier part's, which has no data rows yet (the header was split by the page);
    and not a restart: at most half its row labels (first column) printed in the earlier part already.
    Each joined cell keeps its part (page, table_id) and its index there (index); rows are renumbered.
    tables: _structure.json "tables"; pages: _tables.json "pages" (reading order, text blocks)."""
    norm = lambda s: " ".join((s or "").split()).casefold()
    parts = {(t["page"], t["table_id"]): t for t in tables}
    info = {p["page"]: p for p in pages}

    def heads(t):  # header rows (contiguous from the top) as [(col, text), ...] per row
        rows, n = {c["row"] for c in t["cells"] if c["column_header"]}, 0
        while n in rows:
            n += 1
        return [sorted((c["col"], norm(c.get("text"))) for c in t["cells"] if c["row"] == r) for r in range(n)]

    def labels(t):
        n = len(heads(t))
        return [x for c in t["cells"] if c["col"] == 0 and c["row"] >= n and (x := norm(c.get("text")))]

    def edges(t):  # [left edge, right edge of each column] in PDF points
        rights = [max((c["bbox_pdf_pt"][2] for c in t["cells"] if c["col"] == q and c["col_span"] == 1
                       and c.get("bbox_pdf_pt")), default=None) for q in range(t["num_cols"])]
        lefts = [c["bbox_pdf_pt"][0] for c in t["cells"] if c["col"] == 0 and c.get("bbox_pdf_pt")]
        return None if None in rights or not lefts else [min(lefts), *rights]

    def drop(g, last, f):
        """Header rows of f to leave out when it continues g (last: g's part on the page before), or None."""
        # ponytail: upright tables only; a sideways table's columns run down the page (compare y edges if needed)
        if f.get("orientation") or last.get("orientation") or not f.get("complete") or f["num_cols"] != g["num_cols"]:
            return None
        a, b = edges(last), edges(f)
        if not a or not b or any(abs(x - y) > COL_TOL for x, y in zip(a, b)):
            return None
        seen, new = set(labels(g)), labels(f)
        if new and sum(x in seen for x in new) > len(new) / 2:
            return None  # the row labels start over: another table with the same columns
        hg, hf = heads(g), heads(f)
        if not hf:
            return 0
        if hf == hg:
            return len(hf)
        return 0 if len(hg) == g["num_rows"] else None

    def add(g, f, skip):
        for i, c in enumerate(f["cells"]):
            if c["row"] >= skip:
                g["cells"].append({**c, "row": c["row"] - skip + g["num_rows"], "page": f["page"],
                                   "table_id": f["table_id"], "index": i})
        g["num_rows"] += f["num_rows"] - skip
        g["parts"].append({"page": f["page"], "table_id": f["table_id"]})

    groups, open_ = [], None  # open_: (joined table, its last part) when that part ends its page
    for n in sorted(info):
        p = info[n]
        seq = sorted([("table", d) for d in p["detections"] if d["is_table"] and "order" in d]
                     + [("text", b) for b in p["text_blocks"] if b["label"] not in RUNNING and "order" in b],
                     key=lambda x: x[1]["order"])
        cur = None
        if open_ and open_[1]["page"] == n - 1:
            top = open_[1]["bbox_pdf_pt"][1]
            above = {norm(b.get("text")) for b in info[n - 1]["text_blocks"] if b["bbox_pdf_pt"][3] <= top}
            i = 0
            while i < len(seq) and seq[i][0] == "text" and norm(seq[i][1].get("text")) in above - {""}:
                i += 1
            f = parts.get((n, seq[i][1]["id"])) if i < len(seq) and seq[i][0] == "table" else None
            if f and (k := drop(open_[0], open_[1], f)) is not None:
                add(open_[0], f, k)
                cur = (open_[0], f)
        last = parts.get((n, seq[-1][1]["id"])) if seq and seq[-1][0] == "table" else None
        if last is None:
            open_ = None
        elif cur and cur[1] is last:
            open_ = cur
        else:
            g = {"parts": [], "title": last.get("title"), "subtitle": last.get("subtitle"), "num_rows": 0,
                 "num_cols": last["num_cols"], "cells": []}
            add(g, last, 0)
            groups.append(g)
            open_ = (g, last)
    return [g for g in groups if len(g["parts"]) > 1]


def selfcheck():
    """VLM table markup: a header cell spanning two columns over a 2-column body, text with '<' kept whole."""
    text = "<fcel>Revenue<lcel><nl><fcel>2024<fcel>< 5%<nl>"
    assert otsl(text) == (["fcel", "lcel", "nl", "fcel", "fcel", "nl"], ["Revenue", "2024", "< 5%"]), otsl(text)
    cells, nr, nc = structure.build_cells(otsl(text)[0], [], [0, 0, 10, 10])
    assert (nr, nc, [c["col_span"] for c in cells]) == (2, 2, [2, 1, 1]), cells
    assert otsl_rows(text) == "Revenue | \n2024 | < 5%", otsl_rows(text)
    assert otsl_rows("plain OCR text") == "plain OCR text"
    # a scan's hidden OCR text is dropped (the page is then read from its image); printed text and drawings stay,
    # and a page with no hidden text is not touched
    with pymupdf.open() as doc:
        scan = doc.new_page()
        scan.insert_text((72, 100), "Printed stamp")
        scan.insert_text((72, 100), "BAL^NCEs overlapping", render_mode=3)
        scan.insert_text((72, 200), "t999 alone", render_mode=3)
        scan.draw_rect(pymupdf.Rect(50, 300, 200, 400))
        doc.new_page().insert_text((72, 100), "Born digital")
        assert printed_only(doc) == [1]
        assert [p.get_text().split() for p in doc] == [["Printed", "stamp"], ["Born", "digital"]]
        assert len(doc[0].get_drawings()) == 1
    t = chart_table("| Year | Revenue | Cost |\n|---|---|---|\n| 2023 | 120 | 80 |\n2024 | 150\n")
    assert t == {"num_rows": 3, "num_cols": 3, "header": ["Year", "Revenue", "Cost"],
                 "rows": [["2023", "120", "80"], ["2024", "150", ""]]}, t
    assert chart_table("") == {"num_rows": 0, "num_cols": 0, "header": [], "rows": []}
    # chart check: a value read twice but printed once is caught (and the printed one it left out is not needed)
    chart = lambda *rows: {"header": ["Label", "Value"], "rows": [list(r) for r in rows]}
    assert unprinted(chart(("A", "26.1"), ("B", "26.1"), ("C", "5.7")), "A\n10.4%\nB\n26.1%\nC\n5.7%\n") == ["26.1"]
    # every value printed: verified (a right value under the wrong label is not caught)
    assert unprinted(chart(("A", "23.6"), ("B", "17.8")), "17.8%\nA\n23.6%\nB\n") == []
    # compared by value: a thousands space or the other decimal mark is the same number, a lost decimal mark is not
    assert unprinted(chart(("A", "3402"), ("B", "13.8")), "3 402\n13,8\n") == []
    assert unprinted(chart(("A", "178"),), "17,8\n") == ["178"]
    assert values("1.234,5") == {1234.5} and values("2,151") == {2151, 2.151} and values("3 402") == {3402}
    # read_chart: a read with a number not printed is read again at the next scale and the better one kept; a
    # verified first read is not read again; with no text layer there is nothing to compare, so one read
    global vlm
    real, answers = vlm, {}
    vlm = lambda image, task: (answers[image], True)
    try:
        printed, block = "A\n10.4%\nB\n26.1%\n", {"label": "chart"}
        answers.update(sharp="L | V\nA | 26.1\nB | 26.1", plain="L | V\nA | 10.4\nB | 26.1")
        r = read_chart(["sharp", "plain"], printed, block)
        assert (r["reads"], r["crop_dpi"], r["check"]["status"]) == (2, 144, "verified"), r
        assert r["chart_table"]["rows"] == [["A", "10.4"], ["B", "26.1"]], r
        assert r["read_by"] == f"{VLM} · Chart Recognition:", r
        r = read_chart(["plain", "sharp"], printed, block)
        assert (r["reads"], r["crop_dpi"], r["check"]["status"]) == (1, 216, "verified"), r
        answers.update(plain="L | V\nA | 99\nB | 98")  # both wrong: the one with fewer numbers not printed
        assert read_chart(["sharp", "plain"], printed, block)["check"]["not_in_pdf"] == ["26.1"]
        assert read_chart(["sharp", "plain"], "A\nB\n", block)["reads"] == 1  # an image: nothing to compare
    finally:
        vlm = real
    # a paragraph keeps its text-layer text; a signature or an icon is an empty placeholder, not read
    para, pic = {"label": "paragraph", "text": "a b", "words": 2}, {"label": "picture", "text": "logo", "words": 1}
    sign = {**pic, "figure_class": "signature"}
    assert region(None, para)["read_by"] == "PDF text layer" and region(None, para)["text"] == "a b"
    assert region(None, sign)["text"] == "" and region(None, sign)["read_by"] == "not read (picture)"
    # read from the image: charts, scans (no text layer) and every picture but a signature or an icon
    assert [needs_vlm(b) for b in (para, sign, {**pic, "figure_class": "icon"}, pic, {**pic, "figure_class": "logo"},
                                   {**pic, "figure_class": "table"}, {"label": "chart", "text": "x"},
                                   {"label": "paragraph", "text": ""})] == [False, False, False, True, True, True, True, True]
    # a QR code is decoded (no VLM), a QR code that decodes to nothing is no guess either
    qr = cv2.resize(cv2.QRCodeEncoder.create().encode("https://lab.example/verify?r=123"), None, fx=6, fy=6,
                    interpolation=cv2.INTER_NEAREST)
    got = read(Image.fromarray(qr), {**pic, "figure_class": "qr_code"})
    assert (got["text"], got["codes"], got["read_by"]) == ("https://lab.example/verify?r=123",
                                                           ["https://lab.example/verify?r=123"], "OpenCV code decoder")
    assert read(Image.new("RGB", (60, 60), "white"), {**pic, "figure_class": "qr_code"})["text"] == ""
    # the same image and task twice: one VLM request
    calls, real = [], ask_vlm
    globals()["ask_vlm"] = lambda buf, task: calls.append(task) or ("Sterling Accuris", True)
    try:
        logo = Image.new("RGB", (40, 20), "purple")
        assert [vlm(logo, "OCR:") for _ in range(2)] == [("Sterling Accuris", True)] * 2 and calls == ["OCR:"]
        assert read(logo, {**pic, "figure_class": "logo"})["text"] == "Sterling Accuris"
    finally:
        globals()["ask_vlm"] = real
        VLM_SEEN.clear()
    order = reading_order([{"page": 1, "table_id": 1, "order": 2, "bbox_pdf_pt": [50, 300, 500, 400]}],
                          [{"page": 1, "id": 2, "label": "paragraph", "order": 1, "bbox_pdf_pt": [50, 120, 500, 200]},
                           {"page": 1, "id": 1, "label": "heading", "order": 0, "bbox_pdf_pt": [50, 80, 500, 100]},
                           {"page": 2, "id": 1, "label": "chart", "bbox_pdf_pt": [50, 80, 500, 300]}])
    assert order == [{"page": 1, "items": [["heading", 1], ["paragraph", 2], ["table", 1]]},
                     {"page": 2, "items": [["chart", 1]]}], order
    assert clean("xx.\x07Loans a​b\n") == "xx.Loans ab\n"
    assert clean("क्‍ष") == "क्‍ष"  # ZWJ kept: the conjunct stays
    # filler typed as text goes, values with dashes stay
    assert clean("--------------------------------Mild") == "Mild" and clean("------") == "" and clean("Total......12") == "Total12"
    assert [clean(s) for s in ("-", "--", "---", "60 - 181", "Hypothyroid -0.0- 4.5", "1.5", "a_b")] == \
           ["-", "--", "---", "60 - 181", "Hypothyroid -0.0- 4.5", "1.5", "a_b"]
    # a two-column spread (page 277 of JSW.pdf): each column whole, its title + subtitle right before its table
    spread = [("page_header", [50, 20, 400, 35], 4), ("section_header", [60, 60, 500, 90], 3),
              ("section_header", [800, 60, 1300, 90], 6), ("text", [60, 95, 250, 110], 5),
              ("text", [800, 95, 1000, 110], 6), ("table", [55, 130, 640, 900], 0),
              ("table", [800, 130, 1390, 860], 0), ("text", [60, 920, 500, 935], 7),
              ("text", [800, 880, 1200, 895], 7), ("page_footer", [50, 980, 200, 995], 2)]
    seq = page_order((1450, 1000), [(l, b) for l, b, _ in spread])
    assert seq == [0, 1, 3, 5, 7, 2, 4, 6, 8, 9], seq
    assert table_titles([spread[i] for i in seq]) == {3: (1, 2), 7: (5, 6)}
    # four text columns (page 210): column by column; a heading straight before a table is its title
    cols = [("section_header", [60, 60, 360, 90], 9), ("table", [60, 92, 360, 99], 0)]
    cols += [("text", [60 + c * 350, 100 + k * 200, 360 + c * 350, 280 + k * 200], 50) for c in range(4) for k in range(4)]
    assert page_order((1450, 1000), [(l, b) for l, b, _ in cols]) == list(range(18))
    assert table_titles(cols) == {1: (0, None)}
    # a heading in another column, or a long paragraph in between, is no title
    assert table_titles([("section_header", [800, 60, 1300, 90], 3), ("table", [55, 130, 640, 900], 0)]) == {}
    assert table_titles([("section_header", [60, 60, 500, 90], 3), ("text", [60, 95, 500, 125], 40),
                         ("table", [55, 130, 640, 900], 0)]) == {}
    # spread: two numbers in a row's last cell go into the empty cell before it; a thousands space, label text and a
    # cell with no empty cell before it are left alone
    t = grid(*otsl("<fcel>Sub<ecel><fcel>1.234,56 7.890,12<nl><fcel>A<ecel><fcel>3 402<nl>"
                   "<fcel>of which 5,00 (prior: 1,00)<ecel><ecel><nl><fcel>B<fcel>1,00<fcel>2,00 3,00<nl>"), [0, 0, 10, 10])
    rows = [[c["text"] for c in t["cells"] if c["row"] == r] for r in range(4)]
    assert rows == [["Sub", "1.234,56", "7.890,12"], ["A", "", "3 402"],
                    ["of which 5,00 (prior: 1,00)", "", ""], ["B", "1,00", "2,00 3,00"]], rows
    # join_pages: a header split by the page break joins (the running header repeated above the next part skipped);
    # a reprinted header is dropped; a paragraph after the table ends it there
    def part(page, rows, head, y=100):
        x = (50, 300, 400, 500)
        cells = [{"row": r, "col": q, "row_span": 1, "col_span": 1, "column_header": r < head, "text": v,
                  "bbox_pdf_pt": [x[q], y + 10 * r, x[q + 1], y + 10 * r + 10]}
                 for r, row in enumerate(rows) for q, v in enumerate(row)]
        return {"page": page, "table_id": 1, "num_rows": len(rows), "num_cols": 3, "complete": True, "orientation": 0,
                "bbox_pdf_pt": [x[0], y, x[-1], y + 10 * len(rows)], "cells": cells}

    def pg(n, *items):  # (label, y0, text); label "table": the page's table #1
        return {"page": n, "detections": [{"id": 1, "is_table": True, "order": k} for k, it in enumerate(items)
                                          if it[0] == "table"],
                "text_blocks": [{"id": k, "label": l, "order": k, "text": s, "bbox_pdf_pt": [50, y, 500, y + 10]}
                                for k, (l, y, s) in enumerate(items) if l != "table"]}

    split = [part(1, [["Category", "Value", "Value"]], 1, 700),
             part(2, [["", "current", "previous"], ["Sales", "38", "-"], ["Rents", "12", "-"]], 1)]
    run = lambda tail: [pg(1, ("heading", 20, "Acme Ltd"), ("table", 700, ""), *tail),
                        pg(2, ("heading", 20, "Acme Ltd"), ("table", 100, ""), ("paragraph", 200, "Costs"))]
    [j] = join_pages(split, run([("page footer", 780, "17")]))
    assert j["parts"] == [{"page": 1, "table_id": 1}, {"page": 2, "table_id": 1}] and j["num_rows"] == 4, j
    assert sorted({c["row"] for c in j["cells"] if c["column_header"]}) == [0, 1], j
    assert join_pages(split, run([("paragraph", 760, "See the notes")])) == []
    again = [part(1, [["Item", "2024", "2023"], ["A", "1", "2"]], 1, 700),
             part(2, [["Item", "2024", "2023"], ["B", "3", "4"]], 1)]
    [j] = join_pages(again, run([]))
    assert j["num_rows"] == 3 and [c["text"] for c in j["cells"] if c["row"] == 2] == ["B", "3", "4"], j


if __name__ == "__main__":
    selfcheck()
    print("content.py self-check ok")
