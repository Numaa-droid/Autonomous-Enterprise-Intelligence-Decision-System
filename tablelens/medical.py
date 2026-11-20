"""A recognition result (_structure.json) -> the lab results of a medical report, one record per test.

    python medical.py      # self-check

reference.py builds the reference JSON's observations and table roles from read(). A lab table is a table with a
test column and a result column; every other table (interpretation grids, trimester ranges) has no roles. Read the
same way for any lab:
  * column roles: test / result / unit / range / flag / method from the header words ("Investigation", "Observed
    Value", "Unit", "Biological Reference Interval" ...). A header row is a row with two or more of those words, so a
    table's continuation (no header of its own, after a page break or a note) is not mistaken for one: with as many
    columns, and results where the table before had them, it takes that table's roles, panel and section. Roles no
    header names come from the cells (numbers, units, ranges).
  * missed tables: a lab header printed outside every recognised table (detection can miss a short table, one test
    above a note) is read as a table of its own: its columns from the header's texts, its rows down to the next
    table, a note heading ("INTERPRETATION :") or a line running across the columns.
  * rows (PDF text layer): Table Transformer sometimes reads two tests as one row. The result cell then has one line
    per test, top-aligned with its test name, and the row is cut at the top of each (row_parts). In the test column
    the lines in the table's name font are names; smaller lines ("Method: ...", "(Serum, Biuret)", "Biochemical") are
    method and specimen, also when they sit above the next test's line in the next row. A row of only a name is a
    section ("HEMOGLOBIN AND RBC INDICES"), unless the PDF prints it in the same text block as the test above (a
    wrapped name: "Mean Corpuscular Hemoglobin" / "Concentration (MCHC)"). Words running on from a test-column line
    into the next column (a long section name) stay with it.
  * panel: the table's title, else what the header prints besides the column names ("Liver" "Function Test Observed
    Value" -> "Liver Function Test"), else the nearest heading above that is not printed on most pages (a logo).
  * result: number with an optional comparator ("< 0.5"), else the printed text ("Pale Yellow"); a printed H / L /
    * / arrow after the number is printed_flag.
  * reference: an interval ("12.0 - 15.0", "< 200", "Below 30") or labelled categories ("Desirable : < 200
    Borderline high : 201 - 240"); the interval is then the normal / desirable / optimal category, or the one for
    the patient's sex. flag: L / H / N against the interval, else from the label of the category the value is in;
    A for a qualitative result where the reference is negative ("Nil", "Absent") and the result is not; null when
    the printed reference does not decide it.
  * patient: sex and age only (they choose sex- and age-specific ranges); no name or ids are copied here.
"""

import re
from collections import Counter
from itertools import takewhile

import reference

LINE_TOL = 3  # points: words whose tops are closer than this are one printed line
SMALLER = 1  # points: a test-column line this much smaller than the table's name font is a method / specimen line
RUN_GAP = 5  # points: a word this close after the one before it on its line is the same printed text
CHUNK_GAP = 8  # points: a header's texts are further apart than this
LINE_GAP = 40  # points: a missed table ends at a vertical gap wider than this
HEADING_REACH = 120  # points: a heading further above the table than this is not its panel
SUMMARY_SHARE = 0.6  # a page with this share of its values restated from other pages is a summary of them
HEADINGS = {"heading", "title"}

ROLES = [  # header words -> column role, checked in this order ("Normal Value" is a range, "Test Result" a result)
    ("range", re.compile(r"referen|range|interval|normal|limits?|expected", re.I)),
    ("result", re.compile(r"observ|result|value|finding|reading", re.I)),
    ("unit", re.compile(r"\bunits?\b|\buom\b", re.I)),
    ("flag", re.compile(r"\bflag|\bstatus|\bremark", re.I)),
    ("method", re.compile(r"\bmethod|\btechnology", re.I)),
    ("test", re.compile(r"investigation|\btests?\b|parameter|analyte|examination|description|\bname\b", re.I)),
]
COLUMN_NAMES = re.compile(r"\b(?:bio(?:logical)?\.?\s+)?ref(?:eren\w*|\.)(?:\s+(?:intervals?|ranges?|values?))?|"
                          r"\bnormal\s+(?:ranges?|values?)|\bobserved(?:\s+values?)?|\btest\s+names?|\b(?:results?|"
                          r"values?|units?|investigations?|parameters?|methods?|methodology|technology|flags?|status|"
                          r"remarks?|ranges?|intervals?)\b", re.I)
ONLY_NAME = re.compile(r"tests?|descriptions?|examinations?|analytes?|names?", re.I)
NOTE_RE = re.compile(r"^(?:interpretations?|notes?|comments?|remarks?|clinical\s+(?:significance|notes?)|impression|"
                     r"advice|disclaimer)\b", re.I)
NUM = r"\d+(?:[.,]\d+)?"
OP = r"(?:<=|>=|≤|≥|<|>)"
SPEC = rf"(?:{OP}\s*{NUM}|{NUM}\s*(?:-|–|—|to)\s*{NUM})"
BOUND_RE = re.compile(rf"(?P<op>{OP})\s*(?P<v>{NUM})|(?P<lo>{NUM})\s*(?:-|–|—|to)\s*(?P<hi>{NUM})", re.I)
RANGE_ONLY = re.compile(rf"{NUM}\s*(?:-|–|—|to)\s*{NUM}", re.I)
WORD_OPS = [(r"\b(?:below|less than|under)\b", "<"), (r"\b(?:up\s*to|upto|not more than)\b", "<="),
            (r"\b(?:above|more than|greater than|over)\b", ">"), (r"\b(?:at least|not less than)\b", ">=")]
LABEL = r"[A-Za-z][A-Za-z /()]*?"
CAT_RE = re.compile(rf"(?P<label>{LABEL})\s*[:\-–]\s*(?P<spec>{SPEC})", re.I)
REV_CAT_RE = re.compile(rf"(?P<spec>{SPEC})\s*:\s*(?P<label>[A-Za-z][A-Za-z /()-]*[A-Za-z)])", re.I)
SEXES = {"male": "male", "men": "male", "m": "male", "female": "female", "women": "female", "f": "female"}
NORMAL_RE = re.compile(r"^(?:desirable|optimal|normal|euthyroid|reference)\b|\blow risk\b", re.I)
RESULT_RE = re.compile(rf"^(?:(?P<flag>HH|LL|H|L|\*+|↑|↓)\s*(?=[<>≤≥\d]))?(?P<op>{OP})?\s*"
                       rf"(?P<num>\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|{NUM})\s*(?P<rest>.*)$")
FLAG_RE = re.compile(r"^\(?(?P<f>HH|LL|H|L|High|Low|Critical|\*+|↑|↓)\)?$", re.I)
NEGATIVE = {"nil", "absent", "negative", "not detected", "none", "non reactive", "non-reactive", "nonreactive",
            "not seen", "neg"}
UNITS = {"%", "fl", "pg", "ml", "mg", "g", "iu", "u", "sec", "secs", "seconds", "ratio", "index", "mmol", "meq",
         "ng", "mcg", "µg", "cells", "copies", "titre", "titer", "mm", "cm", "kg", "bpm", "mmhg"}
SPECIMENS = re.compile(r"^(?:serum|plasma|whole blood|blood|edta(?: blood| whole blood)?|urine|csf|stool|saliva|"
                       r"sputum|fluoride plasma|citrate plasma|capillary blood)$", re.I)
METHODS = re.compile(r"calculat|derived|photometr|immuno(?!glob)|eclia|clia|elisa|hplc|colorimetr|enzymatic|"
                     r"turbidimetr|nephelometr|microscop|chromatograph|electrode|\bise\b|\buv\b|kinetic|flow cytometr|"
                     r"impedance|spectrophotometr|\bvcs|chemilumin|oxidase|bromocresol|biuret|tartrate|arsenazo|"
                     r"uricase|urease|azobilirubin|diazo|mordant|pyridyl|amidohydrolase|lipase|mgcl2|\bgod\b|\bpod\b|"
                     r"\bgpo\b|\bifcc\b|\bp5p\b|\bmeasured\b|\bmethod\s*$", re.I)
NOT_METHOD = re.compile(r"examination|profile|panel", re.I)  # "Microscopic Examination" is a section
INSTRUMENT_RE = re.compile(r"\bretention\s+time|\btime\s*\(min|\bpeak\s+(?:name|area)\b", re.I)
CATEGORY_WORDS = {"desirable", "optimal", "near", "above", "below", "borderline", "high", "low", "very", "normal",
                  "risk", "average", "moderate", "deficiency", "deficient", "insufficiency", "insufficient",
                  "sufficiency", "sufficient", "toxicity", "toxic", "elevated", "acceptable", "ideal", "or", "and"}
METHOD_LINE = re.compile(r"(?:Methods?\s*:|\()", re.I)


def clean(s):
    return " ".join((s or "").split())


def centre(w):
    return (w[0] + w[2]) / 2, (w[1] + w[3]) / 2


def inside(w, box):
    """The word's centre is in the box (x0, y0, x1, y1)."""
    x, y = centre(w)
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def page_words(page):
    """The page's words as (x0, y0, x1, y1, text, block, font size): pymupdf's words, each with the size of the text
    span its centre is in."""
    spans = [(s["bbox"], s["size"]) for b in page.get_text("dict")["blocks"] for ln in b.get("lines", [])
             for s in ln["spans"] if s["text"].strip()]
    return [(*w[:5], w[5], next((size for box, size in spans if inside(w, box)), 0)) for w in page.get_text("words")]


def word_lines(ws):
    """Words -> [(top, words left to right)] top to bottom: words whose tops are within LINE_TOL are one line."""
    out = []
    for w in sorted(ws, key=lambda w: (w[1], w[0])):
        if out and w[1] - out[-1][0] <= LINE_TOL:
            out[-1][1].append(w)
        else:
            out.append((w[1], [w]))
    return [(top, sorted(g, key=lambda w: w[0])) for top, g in out]


def lines(ws):
    """Words -> [(top, text, size, blocks)]: each line's text, its largest font and the PDF text blocks it is in."""
    return [(top, " ".join(w[4] for w in g), max(w[6] for w in g), {w[5] for w in g}) for top, g in word_lines(ws)]


def chunks(ws, gap):
    """A line's words (left to right) -> runs of words less than gap points apart: its separately printed texts."""
    out = []
    for w in ws:
        if out and w[0] - out[-1][-1][2] < gap:
            out[-1].append(w)
        else:
            out.append([w])
    return out


def text_of(ls):
    return clean(" ".join(ln[1] for ln in ls))


# ---------- values ----------

def result_of(text, comma=False):
    """A printed result -> {value, comparator, text, printed_flag, unit}: a number (optional printed flag and comparator
    before it, "H 168.0" / "L < 148"; a printed flag or a unit after it) or, for anything else, value None and the
    text as printed ("Pale Yellow", "Negative")."""
    s = clean(text)
    out = {"value": None, "comparator": None, "text": s or None, "printed_flag": None, "unit": None}
    m = RESULT_RE.match(s)
    if not m or not (parsed := reference.number(m.group("num"), comma)):
        return out
    if RANGE_ONLY.match(s, m.start("num")):
        return out  # "200 - 1000", "0 - 199.99 mg/dL": a range, not one result
    rest = m.group("rest").strip()
    if rest and not (FLAG_RE.match(rest) or unit_like(rest) or (len(rest.split()) == 2 and FLAG_RE.match(
            rest.split()[-1]) and unit_like(rest.split()[0]))):
        return out  # "12 abc": text, not one result
    flag, unit = m.group("flag"), None
    for part in rest.split():
        if FLAG_RE.match(part):
            flag = FLAG_RE.match(part).group("f")
        else:
            unit = part
    return {**out, "value": parsed[0], "comparator": m.group("op"), "printed_flag": flag, "unit": unit}


def unit_like(s):
    s = clean(s)
    return bool(s) and len(s) <= 20 and (s.casefold() in UNITS or ("/" in s and not re.search(r"\d\s*[-–]\s*\d", s)
                                                                    and not re.search(r"[a-z]{3,}\s+[a-z]{3,}", s, re.I)))


def bounds(spec, comma=False):
    """"12 - 15" / "< 200" / ">= 4" -> (low, low_inclusive, high, high_inclusive), or None."""
    m = BOUND_RE.search(spec)
    if not m:
        return None
    v = lambda s: reference.number(s, comma)[0]
    if m.group("lo") is not None:
        return v(m.group("lo")), True, v(m.group("hi")), True
    op, x = m.group("op"), v(m.group("v"))
    return ((None, True, x, op in ("<=", "≤")) if op[0] in "<≤" else (x, op in (">=", "≥"), None, True))


def reference_of(text, sex=None, comma=False):
    """The printed reference -> {low, high, low_inclusive, high_inclusive, categories, expected}: an interval, labelled
    categories (the interval then the normal one, or the patient's sex's), or an expected text ("Clear", "Nil")."""
    s = clean(text)
    out = {"low": None, "high": None, "low_inclusive": True, "high_inclusive": True, "categories": [],
           "expected": None}
    if not s:
        return out
    t = s
    for rx, op in WORD_OPS:
        t = re.sub(rx, op, t, flags=re.I)
    cats = [(clean(m.group("label")).strip(" :-–"), m.group("spec")) for m in CAT_RE.finditer(t)]
    if not cats:
        cats = [(clean(m.group("label")), m.group("spec")) for m in REV_CAT_RE.finditer(t)]
    for label, spec in cats:
        if b := bounds(spec, comma):
            out["categories"].append({"label": label, "low": b[0], "low_inclusive": b[1], "high": b[2],
                                      "high_inclusive": b[3]})
    if out["categories"]:
        by_sex = [c for c in out["categories"] if SEXES.get(c["label"].casefold()) == sex] if sex else []
        normal = [c for c in out["categories"] if NORMAL_RE.search(c["label"])]
        if pick := (by_sex or normal or [None])[0]:
            out.update({k: pick[k] for k in ("low", "low_inclusive", "high", "high_inclusive")})
    elif b := bounds(t, comma):
        out.update(zip(("low", "low_inclusive", "high", "high_inclusive"), b))
    elif not re.search(r"\d", s):
        out["expected"] = s
    return out


def within(v, low, li, high, hi):
    return ((low is None or v > low or (li and v == low)) and (high is None or v < high or (hi and v == high)))


def flag_of(res, ref, comma=False):
    """(flag, interpretation): L / H / N against the reference interval, else from the category the value is in; A for
    a qualitative result where the reference is negative and the result is not; (None, None) when undecided."""
    v, low, high = res["value"], ref["low"], ref["high"]
    if v is None and res["text"] and RANGE_ONLY.fullmatch(res["text"]) and (low is not None or high is not None):
        a, _, b, _ = bounds(res["text"], comma)  # a result printed as a range ("1 - 2" cells/HPF): both ends inside
        return ("L" if not within(a, low, ref["low_inclusive"], None, True) else
                "H" if not within(b, None, True, high, ref["high_inclusive"]) else "N"), None
    if v is None:
        got, want = (res["text"] or "").casefold(), (ref["expected"] or "").casefold()
        if not got or not want:
            return None, None
        if got == want or (got in NEGATIVE and want in NEGATIVE):
            return "N", None
        return ("A", None) if want in NEGATIVE else (None, None)
    cat = next((c["label"] for c in ref["categories"]
                if within(v, c["low"], c["low_inclusive"], c["high"], c["high_inclusive"])), None)
    if low is None and high is None:
        if not cat:
            return None, None
        word = cat.casefold()
        return ("N" if NORMAL_RE.search(cat) else "H" if re.search(r"high|elevated|above|over", word) else
                "L" if re.search(r"low|below|deficien|under", word) else None), cat
    if res["comparator"]:  # "< 0.5": normal only when the whole open side is inside the interval, L / H when outside
        op = res["comparator"]
        ok = (op[0] in "<≤" and low is None and high is not None and v <= high) or \
             (op[0] in ">≥" and high is None and low is not None and v >= low)
        if ok:
            return "N", cat
        if op[0] in "<≤" and low is not None and v <= low:
            return "L", cat
        if op[0] in ">≥" and high is not None and v >= high:
            return "H", cat
        return None, cat
    if not within(v, low, ref["low_inclusive"], None, True):
        return "L", cat
    if not within(v, None, True, high, ref["high_inclusive"]):
        return "H", cat
    return "N", cat


def test_of(text):
    """The test cell -> (name, method, specimen): "Method: X" anywhere and a trailing "(Serum, X)" / "(Calculated)"
    are taken out; an abbreviation in brackets ("(Hb)", "(ALT)") stays in the name."""
    s, method, specimen = clean(text), None, None
    if m := re.search(r"\bMethods?\s*:\s*(.+)$", s, re.I):
        method, s = m.group(1).strip(), s[:m.start()].strip()
    while m := re.search(r"\(([^()]*)\)\s*$", s):
        parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
        if parts and SPECIMENS.match(parts[0]):
            specimen, rest = parts[0], ", ".join(parts[1:])
        elif parts and METHODS.search(m.group(1)):
            rest = m.group(1).strip()
        else:
            break
        method = method or rest or None
        s = s[:m.start()].strip()
    return s.strip(" :-–") or None, method, specimen


def detail_of(text):
    """A method / specimen line printed under a test name -> (method, specimen): "Method: X", "(Serum, X)", "X"."""
    s = clean(text)
    if re.match(r"Methods?\s*:", s, re.I):
        return test_of(s)[1], None
    if s.startswith("("):
        _, method, specimen = test_of(f"x {s}")
        return method or (None if specimen else s.strip("()")), specimen
    return (None, s) if SPECIMENS.match(s) else (s or None, None)


def minor(ln, big):
    """A test-column line that is method / specimen, not a name: smaller than the name font, or "Method:" / "(...)"."""
    return bool(METHOD_LINE.match(ln[1])) or bool(big and ln[2] and ln[2] < big - SMALLER)


def method_line(s):
    """A test-column line naming the method, printed like a name ("Chemiluminescence", "Bromocresol Green Method")."""
    return bool(METHODS.search(s)) and not NOT_METHOD.search(s)


def name_of(ls, big):
    """A test cell's lines -> (name, method, specimen, blocks): the lines in the name font (big) are the name up to a
    method line below the first ("Total Protein" / "Copper tartrate to colour complex"), which with the lines after it
    is the method (down to a note heading: "Summary and Uses:"); the others method / specimen (detail_of); without
    font sizes (no text layer) the text as test_of reads it."""
    main = [ln for ln in ls if not (big and ln[2] and ln[2] < big - SMALLER)]
    k = next((k for k, ln in enumerate(main) if k and method_line(ln[1])), len(main))
    main, after = main[:k], list(takewhile(lambda ln: not ln[1].rstrip().endswith(":"), main[k:]))
    name, method, specimen = test_of(text_of(main))
    method = method or text_of(after) or None
    for ln in ls:
        if ln not in main and ln not in after:
            m, sp = detail_of(ln[1])
            method, specimen = method or m, specimen or sp
    return name, method, specimen, set().union(*(ln[3] for ln in main)) if main else set()


# ---------- tables ----------

def grid_of(t):
    """{(row, col): cell} with each spanning cell in every slot it covers."""
    own = {}
    for c in t["cells"]:
        for r in range(c["row"], min(c["row"] + c["row_span"], t["num_rows"])):
            for q in range(c["col"], min(c["col"] + c["col_span"], t["num_cols"])):
                own.setdefault((r, q), c)
    return own


def distinct(t, own, r):
    """Row r's cells, each spanning cell once."""
    return list({id(own[(r, q)]): own[(r, q)] for q in range(t["num_cols"]) if (r, q) in own}.values())


def role_of_text(s):
    return next((role for role, rx in ROLES if rx.search(s or "")), None)


def header_rows(t, own):
    """Rows that are a header: two or more cells naming a column role (not only flagged column_header)."""
    return {r for r in range(t["num_rows"]) if sum(bool(role_of_text(c.get("text"))) for c in distinct(t, own, r)) >= 2}


def roles_from_header(t, own, header):
    """{col: role} named by the header rows; each role once, the leftmost column that names it."""
    roles = {}
    for q in range(t["num_cols"]):
        s = " ".join(clean(own[(r, q)].get("text")) for r in sorted(header) if (r, q) in own)
        for role, rx in ROLES:
            if rx.search(s) and role not in roles.values():
                roles[q] = role
                break
    return roles


def is_result(s, comma=False):
    return result_of(s, comma)["value"] is not None and not unit_like(s)


def roles_from_cells(t, own, header, roles, comma):
    """The roles no header named, from the body cells: the column most of whose cells are a result / unit / range."""
    roles = dict(roles)
    kinds = {"result": lambda s: is_result(s, comma), "unit": unit_like,
             "range": lambda s: bool(BOUND_RE.search(s)) and result_of(s, comma)["value"] is None}
    for role, test in kinds.items():
        if role in roles.values():
            continue
        share = {q: sum(map(test, ts)) / len(ts) for q in range(1, t["num_cols"])
                 if q not in roles and (ts := body_texts(t, own, header, q))}
        if share and (best := max(share, key=share.get)) is not None and share[best] >= 0.5:
            roles[best] = role
    if "test" not in roles.values() and 0 not in roles:
        roles[0] = "test"
    return roles


def body_texts(t, own, header, q):
    return [s for r in range(t["num_rows"]) if r not in header and (r, q) in own and (s := clean(own[(r, q)].get("text")))]


def results_fit(t, own, header, roles, comma):
    """Most of the cells in the result column are a result (a number, or a word that is not a range): the roles of the
    table before fit this one, which goes on from it."""
    q = next(q for q, role in roles.items() if role == "result")
    ts = body_texts(t, own, header, q) if q < t["num_cols"] else []
    return bool(ts) and sum(is_result(s, comma) or not BOUND_RE.search(s) for s in ts) / len(ts) >= 0.5


def panel_from_header(t, own, header):
    """What the header rows print besides the column names: a panel title read into the header ("Liver" "Function Test
    Observed Value" -> "Liver Function Test"), or None."""
    out = []
    for r in sorted(header):
        for c in distinct(t, own, r):
            s = clean(COLUMN_NAMES.sub(" ", c.get("text") or "")).strip(" :-–.")
            if s and not ONLY_NAME.fullmatch(s) and s not in out:
                out.append(s)
    return " ".join(out) or None


def row_band(r, t, own):
    """(top, bottom) of row r: its cells' top to the next row's top (the table's bottom for the last row). The cell
    boxes alone are too short: a recognised cell's text can include words just below its box."""
    tops = lambda k: [own[(k, q)]["bbox_pdf_pt"][1] for q in range(t["num_cols"])
                      if (k, q) in own and own[(k, q)]["row"] == k and own[(k, q)].get("bbox_pdf_pt")]
    top = min(tops(r), default=t["bbox_pdf_pt"][1])
    below = next((min(x) for k in range(r + 1, t["num_rows"]) if (x := tops(k))), t["bbox_pdf_pt"][3])
    return top, below


def row_parts(r, t, own, words, roles, big):
    """Row r -> (spill, parts): each part {role: lines} is one test. Table Transformer sometimes reads two tests as one
    row: the result cell then holds one line per test, top-aligned with its name. The row is cut at the top of each
    result line that is a number or starts level with a test-name line, and at a name line that follows a method line
    or starts a new text block in capitals (a section after the last test). Every column's words go to the part they
    sit in; words running on from a test-column line stay in the test column. Method / specimen lines above the first
    name are the previous test's: spill. Without a text layer the cells' recognised texts, one part."""
    by_role = {role: own.get((r, q)) for q, role in roles.items()}
    if not words or t.get("orientation") or not all(c and c.get("bbox_pdf_pt") for c in by_role.values()):
        return [], [{role: [(None, clean(c.get("text")) if c else "", 0, set())] for role, c in by_role.items()}]
    top, bottom = row_band(r, t, own)
    xs = {role: (c["bbox_pdf_pt"][0], c["bbox_pdf_pt"][2]) for role, c in by_role.items()}
    owner = {}
    for _, ws in word_lines([w for w in words if top <= centre(w)[1] <= bottom]):
        for k, w in enumerate(ws):
            role = next((role for role, (a, b) in xs.items() if a <= centre(w)[0] <= b), None)
            if k and owner.get(ws[k - 1][:5]) == "test" and w[0] - ws[k - 1][2] < RUN_GAP:
                role = "test"  # the same printed text as the test-column word before it
            if role:
                owner[w[:5]] = role
    col = {role: lines([w for w in words if owner.get(w[:5]) == role]) for role in xs}
    names = [ln for ln in col.get("test", []) if not minor(ln, big)]
    aligned = lambda y: any(abs(y - ln[0]) <= LINE_TOL for ln in names)
    cuts = [ln[0] for ln in col["result"] if result_of(ln[1])["value"] is not None or aligned(ln[0])]
    tests = col.get("test", [])
    for k, ln in enumerate(tests):  # a name line after a method line, or a new block in capitals: a new item
        if k and ln in names and not aligned_any(ln[0], cuts) and (minor(tests[k - 1], big) or (
                not ln[3] & tests[k - 1][3] and ln[1].isupper())):
            cuts.append(ln[0])
    cuts = sorted(cuts)
    first = min([ln[0] for ln in names] + cuts, default=float("inf"))
    spill = [ln for ln in tests if minor(ln, big) and ln[0] < first - LINE_TOL]
    marks = cuts or [top]
    bands = [(m - LINE_TOL if i else -float("inf"), marks[i + 1] - LINE_TOL if i + 1 < len(marks) else float("inf"))
             for i, m in enumerate(marks)]
    parts = [{role: [] for role in col} for _ in bands]
    for role, ls in col.items():
        for ln in ls:
            if role == "test" and ln in spill:
                continue
            parts[next(k for k, (a, b) in enumerate(bands) if a <= ln[0] < b)][role].append(ln)
    return spill, parts


def aligned_any(y, ys):
    return any(abs(y - x) <= LINE_TOL for x in ys)


def heading_above(t, regions, common, body_top):
    """The nearest heading / title region above the table's body on its page (within HEADING_REACH of its top), not
    one printed on most pages, a field label or a column name."""
    top = t["bbox_pdf_pt"][1]
    cands = [b for b in regions if b["page"] == t["page"] and b["label"] in HEADINGS and clean(b.get("text"))
             and b["bbox_pdf_pt"][3] <= body_top + 2 and b["bbox_pdf_pt"][1] >= top - HEADING_REACH
             and clean(b["text"]).casefold() not in common and not NOTE_RE.match(clean(b["text"]))
             and not clean(b["text"]).endswith(":") and not ONLY_NAME.fullmatch(clean(b["text"]))  # "NAME", "REF. :"
             and clean(COLUMN_NAMES.sub(" ", b["text"])).strip(" :-–.")]  # not a column name read as a heading
    return clean(max(cands, key=lambda b: b["bbox_pdf_pt"][3])["text"]) if cands else None


def missed_tables(page, words, tables, width):
    """Lab tables whose header the PDF prints outside every recognised table on the page (detection can miss a short
    table), each as a table: a header row of the header's texts and one body row that row_parts cuts into tests. Its
    columns run between the gaps of the header's texts; its body down to the next table or header, a note heading,
    a line running across two or more columns, or a vertical gap wider than LINE_GAP."""
    boxes = [t["bbox_pdf_pt"] for t in tables if t["page"] == page]
    inside_table = lambda y: any(b[1] - LINE_TOL <= y <= b[3] for b in boxes)
    rows = word_lines(words)
    heads = []
    for k, (y, ws) in enumerate(rows):
        runs = chunks(ws, CHUNK_GAP)
        roles = [role_of_text(" ".join(w[4] for w in c)) for c in runs]
        if not inside_table(y) and {"test", "result"} <= set(roles) and sum(map(bool, roles)) >= 2:
            heads.append((k, runs))
    out = []
    for n, (k, runs) in enumerate(heads):
        edges = [0] + [(a[-1][2] + b[0][0]) / 2 for a, b in zip(runs, runs[1:])] + [width]
        col = lambda x: sum(x > e for e in edges[1:-1])
        top = max(w[3] for w in rows[k][1]) + 0.5
        stops = [b[1] for b in boxes if b[1] > top] + [rows[h][0] for h, _ in heads if rows[h][0] > top]
        bottom, last = min(stops, default=float("inf")), top
        for y, ws in rows[k + 1:]:
            across = any(col(c[-1][2]) - col(c[0][0]) >= 2 for c in chunks(ws, RUN_GAP))
            if y >= bottom or y - last > LINE_GAP or across or NOTE_RE.match(" ".join(w[4] for w in ws)):
                bottom = min(bottom, y - 0.5)
                break
            last = y
        else:
            bottom = min(bottom, last + LINE_GAP)
        if bottom <= top:
            continue
        cell = lambda r, q, s, y0, y1: {"row": r, "col": q, "row_span": 1, "col_span": 1, "column_header": r == 0,
                                        "text": s, "bbox_pdf_pt": [edges[q], y0, edges[q + 1], y1]}
        head_top = min(w[1] for w in rows[k][1])
        out.append({"page": page, "table_id": None, "source_id": f"missed_p{page - 1}_{n + 1:02d}", "orientation": 0,
                    "num_rows": 2, "num_cols": len(runs), "bbox_pdf_pt": [0, head_top, width, bottom],
                    "missed": True, "cells": [cell(0, q, " ".join(w[4] for w in c), head_top, top)
                                              for q, c in enumerate(runs)] +
                                             [cell(1, q, "", top, bottom) for q in range(len(runs))]})
    return out


def patient_of(texts):
    """{sex, age_years} from the report's page texts; nothing that names the patient."""
    sex = age = None
    for s in texts:
        if sex is None and re.search(r"\b(?:gender|sex)\b", s, re.I) and (m := re.search(r"\b(female|male)\b", s, re.I)):
            sex = m.group(1).lower()
        if sex is None and (m := re.search(r"\b\d{1,3}\s*Y(?:ea)?r?s?\s*/\s*([MF])\b", s)):
            sex = SEXES[m.group(1).lower()]
        if age is None and (m := re.search(r"\bAge\b\s*[:/]?\s*(\d{1,3})\s*(?:Y|Yrs?|Years?)\b", s, re.I)):
            age = int(m.group(1))
        if age is None and (m := re.search(r"\b(\d{1,3})\s*Y(?:ea)?r?s?\s*/\s*[MF]\b", s)):
            age = int(m.group(1))
    return {"sex": sex, "age_years": age}


def source_id(t):
    return t.get("source_id") or f"table_p{t['page'] - 1}_{t['table_id']:02d}"


def name_font(t, own, roles, header, words):
    """The font size of the table's test names: the largest in its test column's body, leaving out one-letter words
    (a printed "H" or a stamp's letter reaching into the column) unless there is nothing else."""
    q = next(q for q, role in roles.items() if role == "test")
    cells = [own[(r, q)]["bbox_pdf_pt"] for r in range(t["num_rows"]) if r not in header and (r, q) in own
             and own[(r, q)].get("bbox_pdf_pt")]
    if not cells or not words:
        return 0
    box = (min(b[0] for b in cells), min(b[1] for b in cells), max(b[2] for b in cells), t["bbox_pdf_pt"][3])
    ws = [w for w in words if inside(w, box)]
    return max((w[6] for w in ws if len(w[4]) > 1), default=max((w[6] for w in ws), default=0))


def lab_results(t, own, roles, header, words, patient, comma, results, section, panel, continued):
    """Append table t's tests to results (the list so far: a spill or a wrapped name goes on its last test); the
    section the table ends in."""
    src, sex = source_id(t), patient["sex"]
    big = name_font(t, own, roles, header, words)
    last = lambda: results[-1] if results and results[-1]["page"] == t["page"] else None
    seen = set()  # the printed lines of the parts read so far: Table Transformer can read one row twice
    q_test = next(q for q, role in roles.items() if role == "test")
    lead = None  # the first lines of a name whose cell runs on into the next row, where its result is
    for r in range(t["num_rows"]):
        if r in header:
            continue
        spill, parts = row_parts(r, t, own, words, roles, big)
        if spill and (x := last()):
            for ln in spill:
                m, sp = detail_of(ln[1])
                x["method"], x["specimen"] = x["method"] or m, x["specimen"] or sp
        for k, part in enumerate(parts):
            printed = tuple((ln[0], ln[1]) for role in sorted(part) for ln in part[role] if ln[0] is not None)
            if printed and printed in seen:
                continue
            seen.add(printed)
            name, method, specimen, blocks = name_of(part.get("test", []), big)
            rest = {role: s for role, ls in part.items() if role != "test" and (s := text_of(ls))}
            got = part.get("result", [])
            small = [ln for ln in got if ln[2] and ln[2] < max(g[2] for g in got) - SMALLER
                     and not re.search(r"\d", ln[1])] if len(got) > 1 else []
            if small:  # small print in the result cell is not the result: a method ("SF Cube cell analysis")
                rest["result"], method = text_of([ln for ln in got if ln not in small]), method or text_of(small)
            if not name and part.get("test") and is_result(rest.get("result"), comma):
                name, method, specimen, blocks = name_of(part["test"], 0)  # a result's name, printed smaller
            if name and not rest.get("result") and method_line(name) and (x := last()):
                x["method"], name = x["method"] or name, None  # the test above's method, on a row of its own
            if name and (x := last()) and x["result_text"] is None and \
                    (got := result_of(name, comma))["value"] is not None:
                x.update(value=got["value"], comparator=got["comparator"], result_text=got["text"], _result=got,
                         unit=got["unit"] or x["unit"], printed_flag=got["printed_flag"] or x["printed_flag"])
                x["flag"], x["interpretation"] = flag_of(got, x["reference"], comma)
                continue  # the test above's result, printed in the name column ("7,900 cells/cu.mm")
            if name and NOTE_RE.match(name) and rest.get("result") and (x := last()):
                x["interpretation"] = x["interpretation"] or rest["result"]  # "Interpretation | Non Reactive"
                continue
            if not name and not rest:
                continue
            if not name:  # a wrapped reference line of the test above
                if (x := last()) and "range" in rest:
                    x["reference_text"] = clean(f"{x['reference_text'] or ''} {rest['range']}")
                    x["reference"] = reference_of(x["reference_text"], sex, comma)
                    x["flag"], x["interpretation"] = flag_of(x["_result"], x["reference"], comma)
                elif len(rest) == 1 and big and not re.search(r"\d", s := next(iter(rest.values()))) and \
                        max(ln[2] for ls in part.values() for ln in ls) >= big + SMALLER:
                    section = s  # a section title printed larger over the other columns ("Bilirubin")
                continue
            if not rest:
                if NOTE_RE.match(name):
                    continue
                if (r + 1, q_test) in own and own[(r + 1, q_test)] is own.get((r, q_test)):
                    lead = clean(f"{lead or ''} {name}")  # the name's cell runs on into the next row
                elif (x := last()) and (blocks & x["_blocks"] or name[:1].islower() or x["test"].count("(") >
                                        x["test"].count(")")):  # the test above's name, wrapped onto this row
                    x["test"], x["_blocks"] = f"{x['test']} {name}", x["_blocks"] | blocks
                    x["method"], x["specimen"] = x["method"] or method, x["specimen"] or specimen
                else:
                    section = name
                continue
            if lead:
                name, lead = f"{lead} {name}", None
            tests = [(name, rest)]
            if len(parts) == 1 and (two := second_result(t, own, r, roles, header, comma)):
                tests = [(name, {**{k: v for k, v in rest.items() if k != "range"}, "range": two["range"]}),
                         (f"{name} - {two['label']}" if two["label"] else name,
                          {"result": two["result"], "unit": two["unit"], "range": two["second_range"]})]
            cells = [c for c in distinct(t, own, r) if c.get("bbox_pdf_pt")]
            bbox = [round(f(c["bbox_pdf_pt"][i] for c in cells), 2) for i, f in enumerate((min, min, max, max))] \
                if cells and len(parts) == 1 and not t.get("missed") else None
            for test, got in tests:
                res = result_of(got.get("result"), comma)
                ref = reference_of(got.get("range"), sex, comma)
                flag, interpretation = flag_of(res, ref, comma)
                results.append({
                    "test": test, "value": res["value"], "comparator": res["comparator"], "result_text": res["text"],
                    "unit": got.get("unit") or res["unit"], "reference_text": got.get("range"), "reference": ref,
                    "flag": flag, "interpretation": interpretation,
                    "printed_flag": res["printed_flag"] or got.get("flag"), "method": method or got.get("method"),
                    "specimen": specimen, "panel": panel, "section": section, "page": t["page"],
                    "source_table_id": src, "row_index": r, "part": k if len(parts) > 1 else None, "bbox_pdf_pt": bbox,
                    "continued_table": continued, "missed_by_detection": bool(t.get("missed")), "_result": res,
                    "_blocks": blocks})
    return section


def second_result(t, own, r, roles, header, comma):
    """Row r printing a second result where the table's range column is ("Neutrophils 73 % 40 - 80 | 7716 /cmm
    2000 - 6700" under "Absolute Count") -> {range, label, result, unit, second_range}: the first result's range from
    the cells between its other columns and that one, the second's unit and range from the cells after it, its label
    the nearest text above it in that column; None when the range cell is not a plain number followed by a range."""
    q = next((q for q, role in roles.items() if role == "range"), None)
    text = lambda rr, k: clean(own[(rr, k)].get("text")) if (rr, k) in own else ""
    is_range = lambda s: bool(BOUND_RE.search(s)) and result_of(s, comma)["value"] is None
    if q is None or not is_result(s := text(r, q), comma) or result_of(s, comma)["comparator"]:
        return None
    after = [x for k in range(q + 1, t["num_cols"]) if own.get((r, k)) is not own.get((r, k - 1)) and (x := text(r, k))]
    if not (second := next((x for x in after if is_range(x)), None)):
        return None
    lo = max((k for k, role in roles.items() if k < q), default=-1)
    before = [x for k in range(lo + 1, q) if (x := text(r, k)) and is_range(x)]
    label = next((x for rr in range(r - 1, -1, -1) if rr not in header and (x := text(rr, q))
                  and not is_result(x, comma) and not is_range(x)), None)
    return {"range": before[0] if before else None, "label": label, "result": s,
            "unit": next((x for x in after if unit_like(x)), None), "second_range": second}


def not_lab(t, own, header, roles):
    """A table with a test and a result column that is not a lab table: an instrument's peak table (its top rows name
    a retention time / peak name) or a legend of reference categories (its test column mostly "Desirable",
    "Borderline High" ...)."""
    if any(INSTRUMENT_RE.search(c.get("text") or "") for r in header or range(min(2, t["num_rows"]))
           for c in distinct(t, own, r)):
        return True
    names = body_texts(t, own, header, next(q for q, role in roles.items() if role == "test"))
    legend = [s for s in names if (ws := set(re.findall(r"[a-z]+", s.casefold()))) and ws <= CATEGORY_WORDS]
    return bool(names) and 2 * len(legend) >= len(names)


def restates(x, y):
    """Results x and y are one test printed twice: the same value and a name word in common."""
    words = lambda r: {w for w in re.findall(r"[a-z0-9]+", r["test"].casefold()) if len(w) > 2}
    return x["value"] is not None and x["value"] == y["value"] and bool(words(x) & words(y))


def summary_pages(results):
    """Pages that restate tests read on other pages (a "Tests outside reference range" summary, an insights page):
    at least SUMMARY_SHARE of their (three or more) values are restated from another page. The pages most like that
    go first, so of two pages restating each other one stays."""
    out = set()

    def share(p):
        mine = [x for x in results if x["page"] == p and x["value"] is not None]
        others = [y for y in results if y["page"] not in out | {p}]
        return sum(any(restates(x, y) for y in others) for x in mine) / len(mine) if len(mine) >= 3 else 0

    for p in sorted({x["page"] for x in results}, key=share, reverse=True):
        if share(p) >= SUMMARY_SHARE:
            out.add(p)
    return out


def common_headings(regions):
    """Heading texts printed on most pages (a logo, a running header), casefolded."""
    seen = Counter(clean(b["text"]).casefold() for b in regions if b["label"] in HEADINGS and clean(b.get("text")))
    npages = len({b["page"] for b in regions}) or 1
    return {s for s, n in seen.items() if n > 1 and n >= npages / 2}


def read(structure, doc, words):
    """_structure.json content + the open PDF + {page: page_words} -> {patient, results, tables}. results: one record
    per test (lab_results); tables: every table read, recognised ones and missed_tables alike, by source id ->
    {table, roles, header, panel, lab, continued_from}."""
    tables = [t for t in structure.get("tables", []) if t.get("cells")]
    regions = structure.get("regions", [])
    common = common_headings(regions)
    for p in doc:
        tables += missed_tables(p.number + 1, words[p.number + 1], tables, p.rect.width)
    tables.sort(key=lambda t: (t["page"], t["bbox_pdf_pt"][1]))
    patient = patient_of([p.get_text() for p in doc])
    comma = reference.decimal_comma(tables)

    results, info, prev = [], {}, None  # prev: the last lab table's id, columns, roles, panel and section
    for t in tables:
        own = grid_of(t)
        header = header_rows(t, own)
        continued = (not header and prev is not None and prev["num_cols"] == t["num_cols"]
                     and results_fit(t, own, header, prev["roles"], comma))
        roles = prev["roles"] if continued else roles_from_cells(t, own, header, roles_from_header(t, own, header),
                                                                comma)
        body_top = min((c["bbox_pdf_pt"][1] for r in range(t["num_rows"]) if r not in header
                        for c in distinct(t, own, r) if c.get("bbox_pdf_pt")), default=t["bbox_pdf_pt"][1])
        lab = {"test", "result"} <= set(roles.values()) and not not_lab(t, own, header, roles)
        panel = (prev["panel"] if continued else t.get("title") or (panel_from_header(t, own, header) if lab else None)
                 or heading_above(t, regions, common, body_top))
        info[source_id(t)] = {"table": t, "roles": roles if lab else {}, "header": header, "panel": panel, "lab": lab,
                              "continued_from": prev["id"] if continued else None}
        if not lab:
            continue
        section = lab_results(t, own, roles, header, words.get(t["page"]), patient, comma, results,
                              prev["section"] if continued else None, panel, continued)
        prev = {"id": source_id(t), "num_cols": t["num_cols"], "roles": roles, "panel": panel, "section": section}

    results = [x for x in results if x["result_text"] is not None]  # no result: a sub-heading ("Differential Count")
    summaries = summary_pages(results)
    for x in (x for x in results if x["page"] in summaries and x["reference_text"]):
        if y := next((y for y in results if y["page"] not in summaries and not y["reference_text"]
                      and restates(x, y)), None):  # the summary prints the range its test's own page does not
            y["reference_text"] = x["reference_text"]
            y["reference"] = reference_of(x["reference_text"], patient["sex"], comma)
            y["flag"], y["interpretation"] = flag_of(y["_result"], y["reference"], comma)
    results = [x for x in results if x["page"] not in summaries]
    for x in results:
        del x["_result"], x["_blocks"]
    return {"patient": patient, "results": results, "tables": info}


# ---------- self-check ----------

def selfcheck():
    """Results, reference intervals and categories, flags, test text, header panels, a table whose body row Table
    Transformer read as two tests and a section cut apart, and a header detection missed read as a table."""
    r = result_of("12.6")
    assert (r["value"], r["comparator"]) == (12.6, None) and result_of("< 0.5")["comparator"] == "<"
    assert result_of("279000")["value"] == 279000 and result_of("1.677")["value"] == 1.677
    assert result_of("15.2 H")["printed_flag"] == "H" and result_of("4.1 g/dL")["unit"] == "g/dL"
    assert result_of("200 - 1000")["value"] is None and result_of("Pale Yellow")["text"] == "Pale Yellow"
    r = result_of("H 168.0")
    assert (r["value"], r["printed_flag"]) == (168.0, "H") and result_of("L 18.0")["printed_flag"] == "L"
    r = result_of("L < 148")
    assert (r["value"], r["comparator"], r["printed_flag"]) == (148, "<", "L")
    assert result_of("0 - 199.99 mg/dL")["value"] is None and result_of("8.8 - 10.6 mg/dL")["value"] is None
    assert result_of("Low")["value"] is None and result_of("7,900 cells/cu.mm")["value"] == 7900
    assert unit_like("million/uL") and unit_like("/µL") and unit_like("fL") and not unit_like("12.0 - 15.0")

    ref = reference_of("12.0 - 15.0")
    assert (ref["low"], ref["high"]) == (12.0, 15.0) and flag_of(result_of("12.6"), ref) == ("N", None)
    assert flag_of(result_of("63.9"), reference_of("50 - 62"))[0] == "H"
    assert flag_of(result_of("200"), reference_of("< 200"))[0] == "H"
    assert flag_of(result_of("H 168.0"), reference_of("< 150"))[0] == "H"
    assert flag_of(result_of("L < 148"), reference_of("187 - 833"))[0] == "L"
    assert flag_of(result_of("> 1000"), reference_of("0 - 87"))[0] == "H"
    assert flag_of(result_of("< 150"), reference_of("100 - 200"))[0] is None  # could be either side of 100
    assert flag_of(result_of("36.08"), reference_of("Below 30"))[0] == "H"
    assert flag_of(result_of("0"), reference_of("< 200"))[0] == "N"
    assert flag_of(result_of("1 - 2"), reference_of("0 - 2"))[0] == "N"
    assert flag_of(result_of("4 - 6"), reference_of("0 - 2"))[0] == "H"
    chol = reference_of("Desirable : < 200 Borderline high : 201 - 240 High : > 240")
    assert [c["label"] for c in chol["categories"]] == ["Desirable", "Borderline high", "High"] and chol["high"] == 200
    assert flag_of(result_of("217.83"), chol) == ("H", "Borderline high")
    hdl = reference_of("Low :< 40 High : > 60")
    assert flag_of(result_of("38.49"), hdl) == ("L", "Low") and flag_of(result_of("50"), hdl) == (None, None)
    ratio = reference_of("Desirable/Low Risk : 3.3 - 4.4 Borderline/Middle Risk : 4.5 - 7.1")
    assert flag_of(result_of("5.66"), ratio) == ("H", "Borderline/Middle Risk")
    hb = reference_of("Male : 13 - 17 Female : 12 - 15", sex="female")
    assert (hb["low"], hb["high"]) == (12, 15)
    assert reference_of("<13.0 : Suspected Hemoglobinopathy")["categories"][0]["label"] == "Suspected Hemoglobinopathy"
    assert flag_of(result_of("Hazy"), reference_of("Clear")) == (None, None)
    assert flag_of(result_of("Present"), reference_of("Absent"))[0] == "A"
    assert flag_of(result_of("Nil"), reference_of("Absent"))[0] == "N"

    assert test_of("Haemoglobin (Hb) Method: Coulter principle") == ("Haemoglobin (Hb)", "Coulter principle", None)
    assert test_of("Triglyceride (Serum, Glycerol Phosphate Oxidase)") == ("Triglyceride", "Glycerol Phosphate Oxidase",
                                                                         "Serum")
    assert test_of("Non HDL Cholesterol (Calculated)") == ("Non HDL Cholesterol", "Calculated", None)
    assert test_of("SGPT (ALT) (Serum, UV with P5P)") == ("SGPT (ALT)", "UV with P5P", "Serum")
    assert detail_of("(Serum, Biuret)") == ("Biuret", "Serum") and detail_of("Biochemical") == ("Biochemical", None)
    assert detail_of("(Calculated)") == ("Calculated", None) and detail_of("Method: VCSn") == ("VCSn", None)
    ln = lambda y, s, size=10: (y, s, size, {0})
    assert name_of([ln(0, "Total Protein"), ln(10, "Copper tartrate to colour complex")], 10)[:2] == \
           ("Total Protein", "Copper tartrate to colour complex")
    assert name_of([ln(0, "Homocysteine"), ln(10, "Chemiluminescence"), ln(20, "Summary and Uses:")], 10)[:2] == \
           ("Homocysteine", "Chemiluminescence")
    assert name_of([ln(0, "Mean Corpuscular Hemoglobin"), ln(10, "Concentration (MCHC)")], 10)[:2] == \
           ("Mean Corpuscular Hemoglobin Concentration (MCHC)", None)
    assert method_line("Cholesterol oxidase – Peroxidase method") and not method_line("Microscopic Examination")
    rec = lambda page, test, v: {"page": page, "test": test, "value": v}
    summary = [rec(1, "HDL Cholesterol: Be Careful", 33.0), rec(1, "Calcium:", 8.54), rec(1, "Urine Analysis", 0.68)]
    detail = [rec(2, "HDL CHOLESTEROL - DIRECT", 33), rec(2, "CALCIUM", 8.54), rec(2, "TSH", 1.2), rec(2, "T3", 0.9)]
    assert summary_pages(summary + detail) == {1} and summary_pages(detail) == set()
    assert summary_pages(detail + [{**x, "page": 3} for x in detail]) == {2}  # a page printed twice: one stays
    assert patient_of(["Gender : Female Age : 47 Years"]) == {"sex": "female", "age_years": 47}
    assert patient_of(["Age/Sex 52 Y / M"]) == {"sex": "male", "age_years": 52}

    cell = lambda r, q, s, b, head=False: {"row": r, "col": q, "row_span": 1, "col_span": 1, "column_header": head,
                                           "text": s, "bbox_pdf_pt": b}
    t = {"page": 1, "table_id": 1, "num_rows": 2, "num_cols": 3, "orientation": 0, "bbox_pdf_pt": [0, 0, 300, 75],
         "cells": [cell(0, 0, "Liver Investigation", [0, 0, 100, 20], True),
                   cell(0, 1, "Function Test Observed Value", [100, 0, 200, 20], True),
                   cell(0, 2, "Reference Range", [200, 0, 300, 20], True),
                   cell(1, 0, "A B", [0, 20, 100, 40]), cell(1, 1, "1 2", [100, 20, 200, 40]),
                   cell(1, 2, "1 - 3 1 - 3", [200, 20, 300, 40])]}
    w = lambda x, y, s, size=10, block=0: (x, y, x + 10, y + 8, s, block, size)
    words = [w(5, 22, "A"), w(5, 31, "Method:", 8), w(17, 31, "X", 8), w(5, 42, "B", block=1), w(110, 22, "1"),
             w(110, 42, "2"), w(210, 22, "1"), w(225, 22, "-"), w(240, 22, "3"), w(210, 42, "1"), w(225, 42, "-"),
             w(240, 42, "5"), w(5, 51, "Method:", 8, 1), w(17, 51, "Y", 8, 1),
             w(5, 62, "NEXT", block=2), w(17, 62, "SECTION", block=2), w(29, 62, "NAME", block=2),
             w(41, 62, "IS", block=2), w(53, 62, "VERY", block=2), w(65, 62, "LONG", block=2),
             w(77, 62, "AND", block=2), w(89, 62, "RUNS", block=2), w(101, 62, "ON", block=2)]
    own = grid_of(t)
    header = header_rows(t, own)
    roles = roles_from_header(t, own, header)
    assert header == {0} and roles == {0: "test", 1: "result", 2: "range"}
    assert panel_from_header(t, own, header) == "Liver Function Test"
    assert name_font(t, own, roles, header, words + [w(50, 25, "H", 12)]) == 10  # a stray letter is not the name font
    assert not not_lab(t, own, header, roles)
    rows = lambda *rs: {**t, "num_rows": len(rs), "num_cols": 2, "cells": [
        cell(r, q, s, [100 * q, 20 * r, 100 * q + 100, 20 * r + 20])
        for r, row in enumerate(rs) for q, s in enumerate(row)]}
    legend = rows(("DESIRABLE", "<200"), ("BORDERLINE HIGH", "200-239"), ("HIGH", ">240"))
    peaks = rows(("Peak Name", "Retention Time (min)"), ("F", "1.08"), ("P2", "1.30"))
    assert not_lab(legend, grid_of(legend), set(), {0: "test", 1: "result"})
    assert not_lab(peaks, grid_of(peaks), set(), {0: "test", 1: "result"})
    results = []
    section = lab_results(t, own, roles, header, words, {"sex": None}, False, results, None, "P", False)
    assert [(x["test"], x["value"], x["method"], x["reference_text"], x["part"]) for x in results] == \
           [("A", 1, "X", "1 - 3", 0), ("B", 2, "Y", "1 - 5", 1)]
    assert section == "NEXT SECTION NAME IS VERY LONG AND RUNS ON"
    # a continuation page: no header row (its first row only flagged column_header), so no roles of its own
    t2 = {**t, "cells": [cell(0, 0, "C Method: Y", [0, 0, 100, 20], True), cell(0, 1, "4", [100, 0, 200, 20], True),
                         cell(0, 2, "1 - 3", [200, 0, 300, 20], True), cell(1, 0, "D", [0, 20, 100, 60]),
                         cell(1, 1, "2", [100, 20, 200, 60]), cell(1, 2, "1 - 3", [200, 20, 300, 60])]}
    assert not header_rows(t2, grid_of(t2)) and results_fit(t2, grid_of(t2), set(), roles, False)
    # a header and one test that detection missed, then a note running across the page
    page = [w(5, 100, "Investigation"), w(110, 100, "Result"), w(210, 100, "Reference"), w(222, 100, "Range"),
            w(5, 115, "Iron"), w(110, 115, "72"), w(210, 115, "50"), w(225, 115, "-"), w(240, 115, "170"),
            w(5, 140, "INTERPRETATION"), *(w(5 + 12 * k, 152, "word") for k in range(25))]
    missed = missed_tables(1, page, [], 300)
    assert len(missed) == 1 and missed[0]["num_cols"] == 3 and missed[0]["bbox_pdf_pt"][3] < 140
    m = missed[0]
    results = []
    mown = grid_of(m)
    lab_results(m, mown, roles_from_header(m, mown, header_rows(m, mown)), header_rows(m, mown), page,
                {"sex": None}, False, results, None, None, False)
    assert [(x["test"], x["value"], x["reference_text"], x["flag"]) for x in results] == [("Iron", 72, "50 - 170", "N")]


if __name__ == "__main__":
    selfcheck()
    print("medical selfcheck ok")
