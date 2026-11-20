"""A recognition result (_structure.json) -> the reference JSON the LLM reads, for a medical report.

    python reference.py convert <pdf> [--keep-identifiers]  # output/<name>/reference/<name>.reference.json
    python reference.py compare <ours.json> <truth.json>     # how close one reference JSON is to another

app.py runs `convert` after every recognition job. The layout is the reference schema
(schema/medical-report.schema.json), any medical report fits it -- a report without lab tables simply has no tests:
  * document.metadata: file name and size, page count and size, word count (the PDF's text layer), whether it has
    page headers / footers, deidentified.
  * document.patient: sex and age (they choose sex-specific reference ranges); with keep_identifiers also the name
    and ids the report header prints. Without (the default) those values are redacted everywhere ("[REDACTED]"), the
    file name included -- read from the header's label / value pairs, so best effort, not a certified
    de-identification.
  * document.report: the header's dates (collected / received / registered / reported, as printed and as ISO, day
    first) and referring doctor, from the first page that prints each; the sample types the pages print.
  * document.structure: a section per panel (Complete Blood Count, Lipid Profile ...), else per page, its tables.
  * index.paragraphs: para-N, every layout region with text outside the tables, in reading order; role
    sectionHeading / paragraph / listItem / caption / footnote / pageHeader / pageFooter / keyValue / picture (the
    text read from a picture: a logo's or stamp's text, a decoded QR code).
  * index.tables: tbl-N, every table as a full grid -- one cell per (row, column) slot; a spanning cell is repeated
    in each slot it covers, the copies pointing at it with source_cell_id ("<source_table_id>:r<row>:c<col>").
    A table detection missed (medical.py) is rebuilt from its tests (recovered).
  * index.tests: test-N, one per test result (medical.py): name, result, unit, reference range, flag, method,
    specimen (the row's, else the page's sample type), panel, section, and its table, row and box.
order_index: paragraphs 0..N-1, then the tables from N+1.

Fields read from the printed text, the same way for any report:
  * columns: column_role test / result / unit / reference_range / flag / method from the header words ("Investigation",
    "Observed Value", "Unit", "Biological Reference Interval"), null in a table that is no lab table; header: its
    header texts.
  * rows: row_type row_header (the header rows), section ("HEMOGLOBIN AND RBC INDICES"), test (a row with a test
    result), data (a body row of any other table), empty; test_ids: the tests read from the row (two when Table
    Transformer read two printed tests as one row).
  * cells: kind rowHeader (first column) / columnHeader (header rows) / content; cell_type empty / nil / numeric
    ("12.6", "< 0.5") / range ("12 - 15") / text, numeric_value and comparator; label: the row's test name or first
    column text, header: the column's header texts -- so each value reads on its own ("Haemoglobin (Hb)", "Observed
    Value", 12.6, "g/dL"); unit: a numeric cell's unit from the row's unit column; test_id: the test a result cell is.
  * tests: flag H / L / N against the reference interval, else from the label of the category the value is in
    ("Borderline high"); A for a qualitative result where the reference is negative ("Nil", "Absent") and the
    result is not; null when the printed reference does not decide it.
Boxes are polygons in PDF points (bounding_regions), pages from 1.
"""

import argparse
import itertools
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import pymupdf

OUT = Path(__file__).with_name("output")
SCHEMA = "medical-report/1.0"
SCHEMA_FILE = Path(__file__).with_name("schema") / "medical-report.schema.json"
REDACTED = "[REDACTED]"
NUMBER = re.compile(r"\d{1,3}(?:[ ,.'  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
NIL = {"-", "–", "—", "−", "n/a", "na", "n.a.", "nil", "not done", "nd", "n.d.", "pending", "--", "---"}
ROLE_OF = {"heading": "sectionHeading", "title": "sectionHeading", "paragraph": "paragraph", "list item": "listItem",
           "caption": "caption", "footnote": "footnote", "page header": "pageHeader", "page footer": "pageFooter",
           "key-value": "keyValue", "form": "keyValue", "checkbox": "keyValue", "picture": "picture"}
NOT_TEXT = {"chart", "table"}  # a picture with text read from it (a logo's, a stamp's, a decoded QR code) is a paragraph
COLUMN_ROLES = {"range": "reference_range"}  # medical.py's column role names -> the schema's

DATE_RE = re.compile(r"\d{1,2}[-/. ](?:[A-Za-z]{3,9}|\d{1,2})[-/. ,]+\d{2,4}(?:,?\s+\d{1,2}:\d{2}(?::\d{2})?"
                     r"(?:\s*[AaPp]\.?[Mm]\.?)?)?|\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?")
DATE_FORMATS = [f"{d}{t}" for d in ("%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
                                    "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d")
                for t in ("", " %H:%M", " %H:%M:%S", " %I:%M %p", " %I:%M:%S %p")]
REPORT_FIELDS = {  # header label (the whole printed text, ":" aside) -> report field
    "collected_at": re.compile(r"(?:sample\s+)?collect(?:ed|ion)\s*(?:on|date|time|date\s*(?:&|and)\s*time)?", re.I),
    "received_at": re.compile(r"(?:sample\s+)?receiv(?:ed|ing)\s*(?:on|date|time)?", re.I),
    "registered_at": re.compile(r"reg(?:n|istration|istered|d)?\.?\s*(?:on|date|time)", re.I),
    "reported_at": re.compile(r"report(?:ed|ing)?\s*(?:on|date|time)", re.I),
    "specimen_type": re.compile(r"(?:sample|specimen)\s*type", re.I),
    "referred_by": re.compile(r"ref(?:\.|erred)?\s*by(?:\s*dr\.?)?|referring\s+(?:doctor|physician)", re.I),
}
IDENTIFIERS = {  # header label -> patient identifier kind (redacted unless keep_identifiers)
    "name": re.compile(r"(?:patient(?:'s)?\s*)?name|patient", re.I),
    "patient_id": re.compile(r"(?:patient|pt\.?)\s*(?:id|no\.?|number)|uhid|mrn|cr\s*no\.?|ip\s*no\.?|op\s*no\.?", re.I),
    "visit_id": re.compile(r"vid|visit\s*(?:id|no\.?)|lab\s*(?:id|no\.?)|accession(?:\s*no\.?)?|barcode|sample\s*id|"
                           r"reg(?:istration)?\s*no\.?|bill\s*no\.?|order\s*(?:id|no\.?)", re.I),
    "phone": re.compile(r"(?:phone|mobile|mob\.?|contact)(?:\s*no\.?)?", re.I),
    "address": re.compile(r"address", re.I),
    "email": re.compile(r"e-?mail", re.I),
    "date_of_birth": re.compile(r"d\.?o\.?b\.?|date\s+of\s+birth", re.I),
    "national_id": re.compile(r"aadhaar|aadhar|ssn|passport(?:\s*no\.?)?|abha(?:\s*(?:id|no\.?))?", re.I),
}


def reference_json(stem):
    return OUT / stem / "reference" / f"{stem}.reference.json"


def norm(s):
    return " ".join((s or "").split()).casefold()


def polygon(b):
    x0, y0, x1, y1 = (round(v, 2) for v in b)
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def region_of(page, box):
    return [{"page_number": page, **({"polygon": polygon(box)} if box else {})}]


# ---------- values ----------

def number(text, decimal_comma=False):
    """One printed number -> (value, negative, percent); None when the text is not one number. "1,783" 1783 · "5.0"
    5.0 · "-2.5" -2.5 · "12%" 12. One separator before exactly 3 digits is a thousands separator unless it is the
    document's decimal separator."""
    s = " ".join(text.split()).rstrip("*").strip()
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    neg = s[:1] in ("-", "–", "−")
    s = s[1:].strip() if neg else s
    if not NUMBER.fullmatch(s):
        return None
    digits = re.sub(r"[ '  ]", "", s)
    seps = re.findall(r"[.,]", digits)
    if not seps:
        v = int(digits)
    elif len(set(seps)) == 2:  # both: the last one is the decimal separator
        v = float(digits.replace(seps[0], "").replace(seps[-1], "."))
    elif len(seps) > 1:  # one kind, several times: thousands
        v = int(digits.replace(seps[0], ""))
    else:
        whole, tail = digits.split(seps[0])
        thousands = len(tail) == 3 and seps[0] != ("," if decimal_comma else ".")
        v = int(whole + tail) if thousands else float(f"{whole}.{tail}")
    return (-v if neg else v), neg, pct


def decimal_comma(tables):
    """True when the document's tables print decimals with a comma ("17,8") more often than with a dot ("17.8")."""
    texts = [c.get("text") or "" for t in tables for c in t["cells"]]
    count = lambda rx: sum(len(re.findall(rx, s)) for s in texts)
    return count(r"\d,\d{1,2}(?!\d)") > count(r"\d\.\d{1,2}(?!\d)")


def date_of(text):
    """A printed date (and time) -> {text, iso}; iso None when no known day-first / ISO format reads it."""
    s = " ".join(text.replace(",", " ").split())
    s = re.sub(r"\s*([AaPp])\.?([Mm])\.?$", lambda m: f" {m.group(1).upper()}{m.group(2).upper()}", s)
    for f in DATE_FORMATS:
        try:
            d = datetime.strptime(s, f)
        except ValueError:
            continue
        return {"text": text.strip(), "iso": d.isoformat(timespec="minutes") if "%H" in f or "%I" in f
                else d.date().isoformat()}
    return {"text": text.strip(), "iso": None}


def cell_value(s, comma):
    """(cell_type, numeric_value, comparator): empty / nil / numeric ("12.6", "< 0.5") / range ("12 - 15") / text."""
    import medical
    if not s:
        return "empty", None, None
    if norm(s) in NIL:
        return "nil", None, None
    if (r := medical.result_of(s, comma))["value"] is not None:
        return "numeric", r["value"], r["comparator"]
    return ("range" if medical.RANGE_ONLY.fullmatch(s) else "text"), None, None


# ---------- report header ----------

def header_pairs(words):
    """A page's (label, value) pairs: each printed text and the one after it on its line (medical.chunks), ":" aside."""
    import medical
    out = []
    for _, ws in medical.word_lines(words):
        runs = [s for c in medical.chunks(ws, medical.CHUNK_GAP) if (s := " ".join(w[4] for w in c).strip(" :"))]
        out += list(zip(runs, runs[1:] + [""]))
    return out


def is_label(s):
    return any(rx.fullmatch(s) for rx in (*REPORT_FIELDS.values(), *IDENTIFIERS.values()))


def report_of(words):
    """{page: {field: value}} from each page's header pairs: report fields (a date field needs a date in its value)
    and patient identifiers ("id:<kind>"). A value that is itself a label ("Ref. By" "Sample Type") is no value."""
    fields = [*REPORT_FIELDS.items(), *((f"id:{k}", v) for k, v in IDENTIFIERS.items())]
    pages = {}
    for page, ws in words.items():
        found = {}
        for label, value in header_pairs(ws):
            if not value or is_label(value):
                continue
            field = next((f for f, rx in fields if rx.fullmatch(label)), None)
            if field is None or field in found:
                continue
            if not field.endswith("_at"):
                found[field] = value
            elif m := DATE_RE.search(value):
                found[field] = date_of(m.group(0))
        pages[page] = found
    return pages


def redactor(identifiers):
    """A function that replaces each identifier value (and each word of the name) in a text with REDACTED."""
    terms = {v for v in identifiers.values() if len(v) >= 3}
    terms |= {w for w in re.split(r"\W+", identifiers.get("name", "")) if len(w) >= 3}
    if not terms:
        return lambda s: s
    rx = re.compile("|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.I)
    return lambda s: rx.sub(REDACTED, s) if s else s


# ---------- tests ----------

def test_entry(x, n, specimen, redact):
    """A medical.py result -> its index.tests entry (table_id still the source table id)."""
    import medical
    v, text, ref = x["value"], x["result_text"], x["reference"]
    vtype = ("numeric" if v is not None else "range" if text and medical.RANGE_ONLY.fullmatch(text) else
             "text" if text else "empty")
    low, _, high, _ = medical.bounds(text) if vtype == "range" else (None, None, None, None)
    return {"element_id": f"test-{n}", "name": redact(x["test"]), "panel": x["panel"], "section": x["section"],
            "result": {"content": redact(text) or "", "value_type": vtype, "numeric_value": v,
                       "comparator": x["comparator"], "low": low, "high": high},
            "unit": x["unit"],
            "reference_range": {"content": x["reference_text"] or "", "low": ref["low"], "high": ref["high"],
                                "low_inclusive": ref["low_inclusive"], "high_inclusive": ref["high_inclusive"],
                                "expected": ref["expected"], "categories": ref["categories"]},
            "flag": x["flag"], "interpretation": x["interpretation"], "printed_flag": x["printed_flag"],
            "method": x["method"], "specimen": x["specimen"] or specimen,
            "page_number_estimate": x["page"], "table_id": x["source_table_id"], "row_index": x["row_index"],
            "bounding_regions": region_of(x["page"], x["bbox_pdf_pt"]), "recovered": x["missed_by_detection"]}


# ---------- tables ----------

def cell(page, src, r, q, s, kind, comma, c=None, label=None, header=None, role=None, unit=None, test_id=None):
    """One grid slot -> its cell entry; c the recognised cell covering it (None for a rebuilt table)."""
    ctype, value, comparator = ("text", None, None) if kind == "columnHeader" and s else cell_value(s, comma)
    origin = c is None or (c["row"], c["col"]) == (r, q)
    return {"bounding_regions": region_of(page, c.get("bbox_pdf_pt") if c and s and origin else None),
            "cell_type": ctype, "column_index": q, "column_role": role,
            "column_span": c["col_span"] if c else 1, "row_span": c["row_span"] if c else 1,
            "comparator": comparator, "content": s, "element_id": None, "header": header if kind == "content" else None,
            "kind": kind, "label": label if kind == "content" else None, "numeric_value": value, "row_index": r,
            "source": "pdf", "source_cell_id": None if origin else f"{src}:r{c['row']}:c{c['col']}",
            "test_id": test_id, "unit": unit if ctype == "numeric" else None}


def table_entry(t, info, n, ids, tests_at, redact, comma):
    """One recognised table (medical.read's info for it) -> its index.tables entry. ids: {source id: tbl id};
    tests_at: {(source id, row): [test entries]}."""
    import medical
    src, page, nrows, ncols = medical.source_id(t), t["page"], t["num_rows"], t["num_cols"]
    own = medical.grid_of(t)
    roles = {q: COLUMN_ROLES.get(r, r) for q, r in info["roles"].items()}
    lab = info["lab"]
    header = info["header"] if lab else {r for r in range(nrows) if any(
        own[(r, q)].get("column_header") for q in range(ncols) if (r, q) in own)}
    text = lambda r, q: redact(" ".join(((own.get((r, q)) or {}).get("text") or "").split()))
    heads = {q: " / ".join(dict.fromkeys(s for r in sorted(header) if (s := text(r, q)))) or None for q in range(ncols)}
    col = {role: q for q, role in roles.items()}
    rows = []
    for r in range(nrows):
        tests = tests_at.get((src, r), [])
        kind = lambda q: "columnHeader" if r in header else "rowHeader" if q == 0 else "content"
        label = tests[0]["name"] if len(tests) == 1 else text(r, col.get("test", 0)) or None
        unit = text(r, col["unit"]) if "unit" in col else None
        cells = [cell(page, src, r, q, text(r, q), kind(q), comma, own.get((r, q)), label, heads[q], roles.get(q),
                      unit or (tests[0]["unit"] if len(tests) == 1 else None),
                      tests[0]["element_id"] if len(tests) == 1 and roles.get(q) == "result" else None)
                 for q in range(ncols)]
        filled = [x["column_index"] for x in cells if x["content"]]
        rows.append({"cells": cells, "row_index": r, "test_ids": [x["element_id"] for x in tests],
                     "row_type": "row_header" if r in header else "empty" if not filled else "test" if tests else
                                 "section" if lab and filled == [col.get("test", 0)] else "data"})
    return {"bounding_regions": region_of(page, t["bbox_pdf_pt"]), "caption": redact(t.get("subtitle")),
            "column_count": ncols, "columns": {str(q): {"column_role": roles.get(q), "header": heads[q]}
                                               for q in range(ncols)},
            "continues_table_id": ids.get(info["continued_from"]), "element_id": f"tbl-{n + 1}",
            "heading": redact(t.get("title")), "page_number_estimate": page, "panel": info["panel"],
            "recovered": False, "row_count": nrows, "rows": rows, "source_table_id": src, "table_index": n,
            "table_type": "lab_results" if lab else "other", "test_ids": [x["element_id"] for r in range(nrows)
                                                                          for x in tests_at.get((src, r), [])]}


def recovered_entry(t, info, n, tests, comma):
    """A table detection missed (medical.missed_tables) -> its index.tables entry, rebuilt from its header and tests."""
    src, page, ncols = t["source_id"], t["page"], t["num_cols"]
    roles = {q: COLUMN_ROLES.get(r, r) for q, r in info["roles"].items()}
    heads = [next(c["text"] for c in t["cells"] if c["row"] == 0 and c["col"] == q) for q in range(ncols)]
    field = {"test": lambda x: x["name"], "result": lambda x: x["result"]["content"], "unit": lambda x: x["unit"],
             "reference_range": lambda x: x["reference_range"]["content"], "flag": lambda x: x["printed_flag"],
             "method": lambda x: x["method"]}
    rows = [{"cells": [cell(page, src, 0, q, s, "columnHeader", comma, role=roles.get(q)) for q, s in enumerate(heads)],
             "row_index": 0, "row_type": "row_header", "test_ids": []}]
    for r, x in enumerate(tests, 1):
        cells = [cell(page, src, r, q, (field[roles[q]](x) if roles.get(q) in field else None) or "",
                      "rowHeader" if q == 0 else "content", comma, label=x["name"], header=heads[q],
                      role=roles.get(q), unit=x["unit"], test_id=x["element_id"] if roles.get(q) == "result" else None)
                 for q in range(ncols)]
        rows.append({"cells": cells, "row_index": r, "row_type": "test", "test_ids": [x["element_id"]]})
    return {"bounding_regions": region_of(page, t["bbox_pdf_pt"]), "caption": None, "column_count": ncols,
            "columns": {str(q): {"column_role": roles.get(q), "header": s} for q, s in enumerate(heads)},
            "continues_table_id": None, "element_id": f"tbl-{n + 1}", "heading": None, "page_number_estimate": page,
            "panel": info["panel"], "recovered": True, "row_count": len(rows), "rows": rows, "source_table_id": src,
            "table_index": n, "table_type": "lab_results", "test_ids": [x["element_id"] for x in tests]}


# ---------- document ----------

def convert(structure, doc, file_name, file_size, keep_identifiers=False):
    """_structure.json content + the open PDF -> the reference JSON (a dict)."""
    import medical
    words = {p.number + 1: medical.page_words(p) for p in doc}
    med = medical.read(structure, doc, words)
    comma = decimal_comma([i["table"] for i in med["tables"].values()])

    pages = report_of(words)
    found = {}
    for _, p in sorted(pages.items()):
        for k, v in p.items():
            if k.startswith("id:"):
                found.setdefault(k[3:], v)
    redact = (lambda s: s) if keep_identifiers else redactor(found)
    report = {f: next((p[f] for _, p in sorted(pages.items()) if f in p), None) for f in REPORT_FIELDS
              if f != "specimen_type"}
    report["referred_by"] = redact(report["referred_by"])
    report["specimen_types"] = list(dict.fromkeys(p["specimen_type"] for _, p in sorted(pages.items())
                                                  if "specimen_type" in p))
    age = med["patient"]["age_years"]
    patient = {"sex": med["patient"]["sex"], "age_years": age,
               "name": found.get("name") if keep_identifiers else None,
               "identifiers": {k: v for k, v in found.items() if k != "name"} if keep_identifiers else {}}

    tests = [test_entry(x, n, pages.get(x["page"], {}).get("specimen_type"), redact)
             for n, x in enumerate(med["results"], 1)]
    tests_at, by_table = {}, {}
    for e, x in zip(tests, med["results"]):
        tests_at.setdefault((x["source_table_id"], x["row_index"]), []).append(e)
        by_table.setdefault(x["source_table_id"], []).append(e)

    infos = sorted(med["tables"].items(), key=lambda kv: (kv[1]["table"]["page"], kv[1]["table"].get(
        "order", float("inf")), kv[1]["table"]["bbox_pdf_pt"][1]))
    infos = [(src, i) for src, i in infos if not i["table"].get("missed") or by_table.get(src)]
    ids = {src: f"tbl-{n + 1}" for n, (src, _) in enumerate(infos)}
    tables = [recovered_entry(i["table"], i, n, by_table[src], comma) if i["table"].get("missed") else
              table_entry(i["table"], i, n, ids, tests_at, redact, comma) for n, (src, i) in enumerate(infos)]
    for e in tests:
        e["table_id"] = ids.get(e["table_id"])
    cell_ids = itertools.count(1)
    for t in tables:
        for row in t["rows"]:
            for c in row["cells"]:
                c["element_id"] = f"cell-{next(cell_ids)}"

    # paragraphs: the layout regions with text, in reading order, but not the text inside a table
    common = medical.common_headings(structure.get("regions", []))
    boxes = [(i["table"]["page"], i["table"]["bbox_pdf_pt"]) for _, i in infos]
    in_table = lambda b: any(p == b["page"] and medical.inside(b["bbox_pdf_pt"], bb) for p, bb in boxes)
    regions = [b for b in structure.get("regions", []) if b["label"] not in NOT_TEXT
               and (b.get("text") or "").strip() and not in_table(b)]
    regions.sort(key=lambda x: (x["page"], x.get("order", float("inf")), x["bbox_pdf_pt"][1], x["bbox_pdf_pt"][0]))
    paragraphs = {}
    for i, b in enumerate(regions):
        pid, s = f"para-{i + 1}", redact(" ".join(b["text"].split()))
        role = "pageHeader" if norm(b["text"]) in common else ROLE_OF.get(b["label"], "paragraph")
        paragraphs[pid] = {"bounding_regions": region_of(b["page"], b["bbox_pdf_pt"]), "content": s,
                           "element_id": pid, "is_implicit_heading": role == "sectionHeading",
                           "order_index": i, "role": role}
    for n, t in enumerate(tables):
        t["order_index"] = len(regions) + 1 + n

    # structure: a section per panel (its tables, over pages), else per page
    outline = []
    for t in tables:
        key = ("panel", t["panel"]) if t["panel"] else ("page", t["page_number_estimate"])
        if not outline or outline[-1]["_key"] != key:
            outline.append({"_key": key, "children": [], "heading_source": key[0], "id": f"sec-{len(outline) + 1}",
                            "level": 1, "pages": [], "title": t["panel"], "type": "section"})
        sec = outline[-1]
        sec["children"].append({"ref": t["element_id"], "type": "table"})
        if t["page_number_estimate"] not in sec["pages"]:
            sec["pages"].append(t["page_number_estimate"])
    for sec in outline:
        del sec["_key"]

    first = doc[0].rect if doc.page_count else pymupdf.Rect()
    count = sum(len(w) for w in words.values())
    flagged = [e for e in tests if e["flag"] not in (None, "N")]
    return {"schema_version": SCHEMA,
            "document": {
                "metadata": {"deidentified": not keep_identifiers, "file_name": redact(file_name),
                             "file_size_bytes": file_size, "has_text_layer": count > 0,
                             "has_headers_footers": any(b["label"] in ("page header", "page footer")
                                                        for b in structure.get("regions", [])),
                             "page_count_estimate": doc.page_count,
                             "page_size": {"height_pt": first.height, "width_pt": first.width,
                                           "orientation": "landscape" if first.width > first.height else "portrait"},
                             "test_count": len(tests), "flagged_test_count": len(flagged),
                             "flagged_test_ids": [e["element_id"] for e in flagged], "word_count": count},
                "patient": patient, "report": report, "structure": outline},
            "index": {"paragraphs": paragraphs, "tables": {t["element_id"]: t for t in tables},
                      "tests": {e["element_id"]: e for e in tests}}}


def write(stem, pdf, keep_identifiers=False):
    """output/<stem>/structure/<stem>_structure.json -> output/<stem>/reference/<stem>.reference.json; its path."""
    src = OUT / stem / "structure" / f"{stem}_structure.json"
    structure = json.loads(src.read_text(encoding="utf-8"))
    import content  # here: the app has it loaded; a scan's hidden OCR text is not its text, as in recognition
    with pymupdf.open(pdf) as doc:
        content.printed_only(doc)
        data = convert(structure, doc, structure.get("file") or Path(pdf).name, os.path.getsize(pdf),
                       keep_identifiers)
    path = reference_json(stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")  # replaced in one step: no half-written file
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------- compare ----------

def box(entry):
    p = entry["bounding_regions"][0].get("polygon")
    return [min(p[0::2]), min(p[1::2]), max(p[0::2]), max(p[1::2])] if p else None


def iou(a, b):
    if not a or not b:
        return 0
    i = max(min(a[2], b[2]) - max(a[0], b[0]), 0) * max(min(a[3], b[3]) - max(a[1], b[1]), 0)
    return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i) if i else 0


def compare(ours, truth):
    """Print how close `ours` is to `truth` (both reference JSON): paragraphs by words; tables paired on the same page
    by box overlap (IoU >= 0.5), on pairs with the same shape every field slot by slot; tests paired by name on the
    same page, their result, unit, reference range and flag."""
    o, t = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (ours, truth))
    words = lambda d: Counter(w for p in d["index"]["paragraphs"].values() for w in norm(p["content"]).split())
    ow, tw = words(o), words(t)
    print(f"paragraphs: ours {len(o['index']['paragraphs'])} · truth {len(t['index']['paragraphs'])} · "
          f"truth words found {sum((ow & tw).values()) / max(sum(tw.values()), 1):.1%}")

    OT, TT = o["index"]["tables"].values(), t["index"]["tables"].values()
    used, pairs = set(), []
    for tt in TT:
        cands = [(iou(box(ot), box(tt)), ot["element_id"], ot) for ot in OT
                 if ot["page_number_estimate"] == tt["page_number_estimate"] and ot["element_id"] not in used]
        best = max(cands, key=lambda x: x[0], default=None)
        if best and best[0] >= 0.5:
            used.add(best[1])
            pairs.append((best[2], tt))
    print(f"tables: ours {len(OT)} · truth {len(TT)} · paired {len(pairs)} · truth missed {len(TT) - len(pairs)} · "
          f"ours extra {len(OT) - len(pairs)}")
    same = [(a, b) for a, b in pairs if (a["row_count"], a["column_count"]) == (b["row_count"], b["column_count"])]
    agree, n = Counter(), Counter()
    for a, b in same:
        agree["table_type"] += a["table_type"] == b["table_type"]
        n["table_type"] += 1
        for q, col in b["columns"].items():
            agree["column_role"] += a["columns"][q]["column_role"] == col["column_role"]
            n["column_role"] += 1
        for ra, rb in zip(a["rows"], b["rows"]):
            agree["row_type"] += ra["row_type"] == rb["row_type"]
            n["row_type"] += 1
            for ca, cb in zip(ra["cells"], rb["cells"]):
                for f in ("content", "kind", "cell_type", "numeric_value", "unit", "label"):
                    agree[f] += norm(str(ca[f])) == norm(str(cb[f]))
                    n[f] += 1
    if same:
        print(f"  on the {len(same)} same-shape tables, slot by slot (agreement with truth):")
        for f in ("content", "kind", "cell_type", "numeric_value", "unit", "label", "row_type", "column_role",
                  "table_type"):
            print(f"    {f:<14}{agree[f] / max(n[f], 1):7.1%}  ({agree[f]}/{n[f]})")

    key = lambda x: (x["page_number_estimate"], norm(x["name"]))
    ours_by = {key(x): x for x in o["index"]["tests"].values()}
    matched = [(ours_by[key(x)], x) for x in t["index"]["tests"].values() if key(x) in ours_by]
    print(f"tests: ours {len(o['index']['tests'])} · truth {len(t['index']['tests'])} · paired {len(matched)}")
    for f, get in (("result", lambda x: (x["result"]["numeric_value"], norm(x["result"]["content"]))),
                   ("unit", lambda x: norm(x["unit"])),
                   ("reference", lambda x: (x["reference_range"]["low"], x["reference_range"]["high"])),
                   ("flag", lambda x: x["flag"])):
        if matched:
            k = sum(get(a) == get(b) for a, b in matched)
            print(f"    {f:<14}{k / len(matched):7.1%}  ({k}/{len(matched)})")


# ---------- self-check ----------

def selfcheck():
    """Numbers, dates, redaction, and a small lab report built as a PDF page and converted end to end: report fields
    and identifiers from the header, redacted (or kept), tests with flags, column roles, row types, cell labels."""
    assert number("1,783")[0] == 1783 and number("5.0")[0] == 5.0 and number("-2.5")[:2] == (-2.5, True)
    assert number("1.234")[0] == 1.234 and number("1.234", decimal_comma=True)[0] == 1234 and number("12%")[2]
    assert number("Sales") is None and number("25/24") is None and number("00")[0] == 0
    assert date_of("16-Sep-2025 10:00")["iso"] == "2025-09-16T10:00" and date_of("03/04/2024")["iso"] == "2024-04-03"
    assert date_of("2024-04-03")["iso"] == "2024-04-03" and date_of("5 Mar 2024 2:30 pm")["iso"] == "2024-03-05T14:30"
    assert date_of("Sometime")["iso"] is None
    assert redactor({"name": "Jane Q Doe", "patient_id": "82716"})("Jane Q Doe, Doe J, id 82716") == \
           f"{REDACTED}, {REDACTED} J, id {REDACTED}"

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    for x, y, s in [(40, 60, "Patient Name"), (140, 60, ": Jane Doe"), (330, 60, "Collected On"),
                    (430, 60, ": 16-Sep-2025 10:00"), (40, 80, "Gender"), (140, 80, ": Female"),
                    (330, 80, "Sample Type"), (430, 80, ": Serum"), (40, 100, "Patient ID"), (140, 100, ": 82716"),
                    (40, 140, "Investigation"), (200, 140, "Observed Value"), (330, 140, "Unit"),
                    (430, 140, "Biological Reference Interval"), (40, 160, "Glucose Fasting"), (200, 160, "112"),
                    (330, 160, "mg/dL"), (430, 160, "70 - 99"), (40, 180, "Creatinine"), (200, 180, "0.9"),
                    (330, 180, "mg/dL"), (430, 180, "0.5 - 1.2")]:
        page.insert_text((x, y), s, fontsize=10)
    xs = [(35, 190), (190, 320), (320, 420), (420, 590)]
    grid = [["Investigation", "Observed Value", "Unit", "Biological Reference Interval"],
            ["Glucose Fasting", "112", "mg/dL", "70 - 99"], ["Creatinine", "0.9", "mg/dL", "0.5 - 1.2"]]
    structure = {"file": "Jane_Doe_report.pdf",
                 "regions": [{"page": 1, "label": "key-value", "text": "Patient Name : Jane Doe", "order": 0,
                              "bbox_pdf_pt": [35, 50, 300, 65]},
                             {"page": 1, "label": "picture", "text": "https://lab.example/r?id=82716", "order": 0,
                              "bbox_pdf_pt": [500, 10, 560, 45], "figure_class": "qr_code"},
                             {"page": 1, "label": "picture", "text": "", "order": 0, "bbox_pdf_pt": [440, 10, 490, 45]}],
                 "tables": [{"page": 1, "table_id": 1, "num_rows": 3, "num_cols": 4, "orientation": 0, "order": 1,
                             "title": "BLOOD CHEMISTRY", "bbox_pdf_pt": [35, 128, 590, 186],
                             "cells": [{"row": r, "col": q, "row_span": 1, "col_span": 1, "text": s,
                                        "column_header": r == 0,
                                        "bbox_pdf_pt": [xs[q][0], 128 + 20 * r, xs[q][1], 148 + 20 * r]}
                                       for r, row in enumerate(grid) for q, s in enumerate(row)]}]}
    d = convert(structure, doc, structure["file"], 1234)
    meta = d["document"]["metadata"]
    assert d["schema_version"] == SCHEMA and meta["deidentified"] and meta["file_name"] == f"{REDACTED}_{REDACTED}_report.pdf"
    assert d["document"]["patient"] == {"sex": "female", "age_years": None, "name": None, "identifiers": {}}
    assert d["document"]["report"]["collected_at"]["iso"] == "2025-09-16T10:00"
    assert d["document"]["report"]["specimen_types"] == ["Serum"]
    assert "Jane" not in json.dumps(d) and "82716" not in json.dumps(d)
    tests = list(d["index"]["tests"].values())
    assert [(x["name"], x["result"]["numeric_value"], x["unit"], x["flag"], x["specimen"], x["table_id"])
            for x in tests] == [("Glucose Fasting", 112, "mg/dL", "H", "Serum", "tbl-1"),
                                ("Creatinine", 0.9, "mg/dL", "N", "Serum", "tbl-1")], tests
    assert meta["flagged_test_ids"] == ["test-1"] and meta["test_count"] == 2
    t = d["index"]["tables"]["tbl-1"]
    assert [c["column_role"] for c in t["columns"].values()] == ["test", "result", "unit", "reference_range"]
    assert [r["row_type"] for r in t["rows"]] == ["row_header", "test", "test"] and t["rows"][1]["test_ids"] == ["test-1"]
    v = t["rows"][1]["cells"][1]
    assert (v["kind"], v["label"], v["header"], v["cell_type"], v["numeric_value"], v["unit"], v["test_id"]) == \
           ("content", "Glucose Fasting", "Observed Value", "numeric", 112, "mg/dL", "test-1"), v
    assert d["document"]["structure"][0]["title"] == "BLOOD CHEMISTRY" and t["order_index"] == len(d["index"]["paragraphs"]) + 1
    pics = [p for p in d["index"]["paragraphs"].values() if p["role"] == "picture"]  # a read picture is text, an unread one not
    assert [p["content"] for p in pics] == [f"https://lab.example/r?id={REDACTED}"], pics
    kept = convert(structure, doc, structure["file"], 1234, keep_identifiers=True)
    assert kept["document"]["patient"]["name"] == "Jane Doe"
    assert kept["document"]["patient"]["identifiers"] == {"patient_id": "82716"}
    try:  # both match schema/medical-report.schema.json (when it and jsonschema are there)
        import jsonschema
    except ImportError:
        return
    if not SCHEMA_FILE.exists():
        return
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    for x in (d, kept):
        jsonschema.validate(x, schema, cls=jsonschema.Draft202012Validator)


if __name__ == "__main__":
    selfcheck()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert", help="output/<name>/structure/<name>_structure.json -> output/<name>/reference/")
    c.add_argument("pdf")
    c.add_argument("--keep-identifiers", action="store_true", help="keep the patient's name and ids (default: redacted)")
    k = sub.add_parser("compare", help="ours vs truth, both reference JSON")
    k.add_argument("ours")
    k.add_argument("truth")
    a = ap.parse_args()
    if a.cmd == "convert":
        from app import safe_stem  # here: app loads the web stack
        print("saved", write(safe_stem(Path(a.pdf).name), a.pdf, a.keep_identifiers))
    else:
        compare(a.ours, a.truth)
