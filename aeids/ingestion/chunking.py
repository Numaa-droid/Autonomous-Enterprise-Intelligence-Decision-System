"""TableLens structure -> section-aware chunks with page range, section path and source boxes.

Reading order comes from the structure's ``pages[].items`` (TableLens' page order); without it,
items are sorted by (page, order, top, left).

Section tree. Every heading gets a level (:func:`heading_level`):

    0  PART I … PART IV
    1  Item 1A. / ITEM 7.
    2  financial statement titles, Note 4 – … / NOTE 4 — …, and any other ALL-CAPS heading
       (glossy annual reports such as Microsoft's mark their main sections that way)
    3  any other short heading
    –  a sentence-like "heading" (bold risk-factor leads, footnotes): read as a paragraph

A heading replaces the path at its level and clears the levels below it, so each chunk carries a
path such as ``PART II > Item 7. Management's Discussion … > Segment Operating Performance``.

Rules:

* the section path is the first line of every chunk's text (``Section: …``), so search sees it;
* text never crosses a section boundary, except that a tiny subsection (< ``MIN_CHARS``) is merged
  with the next sibling subsection; the chunk then carries their common parent as its section
  and the subsection headings inline (``meta["subsections"]``);
* short text right before a table in the same section ("The following table shows … (dollars in
  millions):") goes into the table chunk as its context instead of becoming a chunk of its own;
* page headers / footers are skipped (they repeat on every page);
* each table becomes one or more table chunks (split by rows, header repeated), its normalised
  rows in ``meta["table"]``; a table TableLens joined over pages is one table;
* a chart becomes a chart chunk from the data PaddleOCR-VL read off its image. Those values are not in
  the PDF text, so the chunk is flagged ``image_read`` and must not be cited as checked evidence.

Every chunk keeps the boxes (PDF points) of what it was built from in ``meta["boxes"]``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from aeids.ingestion.normalize import (
    SCALES,
    NormRow,
    NormTable,
    TablePartRef,
    decimal_comma,
    normalize_table,
)
from aeids.ingestion.structure import JoinedTable, Region, Structure, Table

CHUNKING_VERSION = "2"
MAX_CHARS = 2000  # body text per chunk (the "Section:" line comes on top)
MIN_CHARS = 400  # below this a subsection is merged with its next sibling
TABLE_CONTEXT_CHARS = 400  # text before a table up to this size becomes the table's context
MAX_HEADING = 120  # longer "headings" are sentences
MAX_PATH_PART = 120
RUNNING = {"page header", "page footer"}
HEADINGS = {"heading", "title"}
CONTEXT_REGIONS = 3  # texts before a table searched for its unit caption
LEAF = 3

ChunkKind = Literal["text", "table", "chart"]

_PART_RE = re.compile(r"^part\s+(?:iv|i{1,3})\b", re.I)
_ITEM_RE = re.compile(r"^item\s+\d{1,2}[a-c]?\b", re.I)
_NOTE_RE = re.compile(r"^note\s+\d{1,2}\b", re.I)
_STATEMENT_RE = re.compile(
    r"^(?:consolidated|combined|standalone)\s+(?:statements?|balance\s+sheets?)\b"
    r"|^notes\s+to\s+(?:the\s+)?(?:consolidated\s+)?financial\s+statements",
    re.I,
)


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def heading_level(text: str) -> int | None:
    """Level of a heading in the section tree (0 = top), or None when it reads as a sentence
    and should be treated as a paragraph."""
    s = _clean(text)
    if not s:
        return None
    if _PART_RE.match(s):
        return 0
    if _ITEM_RE.match(s):
        return 1
    if _STATEMENT_RE.match(s) or _NOTE_RE.match(s):
        return 2
    words = s.split()
    if len(s) > MAX_HEADING or len(words) >= 12 or (s.endswith(".") and len(words) >= 6):
        return None
    letters = [c for c in s if c.isalpha()]
    if len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.9:
        return 2
    return LEAF


def _path_text(path: tuple[str, ...]) -> str | None:
    if not path:
        return None
    parts = [p if len(p) <= MAX_PATH_PART else p[: MAX_PATH_PART - 1].rstrip() + "…" for p in path]
    return " > ".join(parts)


@dataclass
class ChunkDraft:
    kind: ChunkKind
    page_start: int
    page_end: int
    section: str | None
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkResult:
    chunks: list[ChunkDraft]
    tables: list[NormTable]
    warnings: list[str]


def _box(page: int, bbox: list[float] | None, ref: str) -> dict[str, Any]:
    return {"page": page, "bbox": bbox, "ref": ref}


def _split_long(text: str, limit: int) -> list[str]:
    """A text longer than ``limit`` cut at sentence ends (or spaces) into pieces <= limit."""
    if len(text) <= limit:
        return [text]
    pieces, rest = [], text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind(". "), window.rfind("; "), window.rfind("\n"))
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit - 1
        pieces.append(rest[: cut + 1].strip())
        rest = rest[cut + 1 :].lstrip()
    if rest:
        pieces.append(rest)
    return pieces


def _with_section(path: tuple[str, ...], body: str) -> str:
    section = _path_text(path)
    return f"Section: {section}\n\n{body}" if section else body


class _TextBuffer:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.boxes: list[dict[str, Any]] = []
        self.pages: list[int] = []
        self.size = 0
        self.path: tuple[str, ...] = ()
        self.subsections: list[str] = []

    def __bool__(self) -> bool:
        return bool(self.parts)

    def add(self, text: str, page: int, box: dict[str, Any], path: tuple[str, ...]) -> None:
        if not self.parts:
            self.path = path
        self.parts.append(text)
        self.boxes.append(box)
        self.pages.append(page)
        self.size += len(text) + 2

    def add_subheading(self, heading: str, parent: tuple[str, ...]) -> None:
        """Continue into a sibling subsection: the chunk's section becomes the parent and the
        subsection headings stay in the text."""
        if not self.subsections and len(self.path) > len(parent):
            self.subsections.append(self.path[-1])
            self.parts.insert(0, self.path[-1])
        self.path = parent
        self.subsections.append(heading)
        self.parts.append(heading)
        self.size += len(heading) + 2

    def take(self) -> tuple[list[str], list[dict[str, Any]], list[int]]:
        out = (self.parts, self.boxes, self.pages)
        self.__init__()
        return out

    def flush(self) -> ChunkDraft | None:
        if not self.parts:
            return None
        meta: dict[str, Any] = {"kind": "text", "boxes": self.boxes,
                                "section_path": list(self.path)}
        if self.subsections:
            meta["subsections"] = self.subsections
        chunk = ChunkDraft(
            kind="text",
            page_start=min(self.pages),
            page_end=max(self.pages),
            section=_path_text(self.path),
            text=_with_section(self.path, "\n\n".join(self.parts)),
            meta=meta,
        )
        self.__init__()
        return chunk


def _ordered(structure: Structure) -> Iterator[tuple[int, str, int]]:
    """(page, kind, id) in reading order; kind "table" or a region label."""
    if structure.pages:
        for p in sorted(structure.pages, key=lambda p: p.page):
            for kind, ident in p.items:
                yield p.page, kind, ident
        return
    key = lambda page, order, bbox: (page, order if order is not None else 10**9,  # noqa: E731
                                     (bbox or [0, 0])[1], (bbox or [0, 0])[0])
    items = [(key(t.page, t.order, t.bbox_pdf_pt), t.page, "table", t.table_id)
             for t in structure.tables]
    items += [(key(r.page, r.order, r.bbox_pdf_pt), r.page, r.label, r.id)
              for r in structure.regions]
    for _, page, kind, ident in sorted(items):
        yield page, kind, ident


def _unit_summary(table: NormTable) -> str | None:
    seen: dict[tuple[str | None, str | None, str | None], int] = {}
    for row in table.rows:
        for c in row.cells:
            if c.unit:
                k = (c.unit, c.currency, c.scale)
                seen[k] = seen.get(k, 0) + 1
    if not seen:
        return None
    parts = []
    for unit, currency, scale in sorted(seen, key=lambda k: -seen[k]):
        name = {"currency": currency or "currency", "percent": "%", "count": "count"}[unit]
        if scale and scale != "units" and scale in SCALES:
            name += f" {scale}"
        elif unit == "currency" and scale is None:
            name += " (scale not stated)"
        parts.append(name)
    return ", ".join(dict.fromkeys(parts))


def _row_line(row: NormRow) -> str:
    label = row.label + (f" ({', '.join(row.note_refs)})" if row.note_refs else "")
    if row.row_type == "section":
        return f"{label}"
    values = " | ".join(c.text for c in row.cells)
    prefix = f"{row.section}: " if row.section else ""
    return f"{prefix}{label} | {values}"


def _table_chunks(
    table: NormTable,
    path: tuple[str, ...],
    context: list[str],
    context_boxes: list[dict[str, Any]],
    context_pages: list[int],
) -> list[ChunkDraft]:
    head = []
    if table.title:
        head.append(f"Table: {table.title}")
    if table.subtitle:
        head.append(table.subtitle)
    if units := _unit_summary(table):
        head.append(f"Units: {units}")
    cols = [
        f"{name}{f' ({p})' if p else ''}"
        for name, p in zip(table.columns[1:], table.column_periods[1:], strict=True)
        if name or p
    ]
    if cols:
        head.append("Columns: " + " | ".join(cols))
    header = "\n".join(head)
    context_text = "\n".join(context)

    groups: list[list[NormRow]] = [[]]
    size = len(header) + len(context_text)
    for row in table.rows:
        line = len(_row_line(row)) + 1
        if groups[-1] and size + line > MAX_CHARS:
            groups.append([])
            size = len(header)
        groups[-1].append(row)
        size += line

    chunks = []
    for i, rows in enumerate(groups):
        if not rows and i:
            continue
        body = "\n".join(_row_line(r) for r in rows)
        pages = sorted({c.page for r in rows for c in r.cells} or {table.page_start})
        if i == 0:
            pages = sorted(set(pages) | set(context_pages))
        part_table = table.model_copy(update={"rows": rows})
        boxes = [_box(p.page, p.bbox, f"table:{p.page}:{p.table_id}") for p in table.parts]
        lead = f"{context_text}\n" if i == 0 and context_text else ""
        chunks.append(ChunkDraft(
            kind="table",
            page_start=pages[0],
            page_end=pages[-1],
            section=_path_text(path),
            text=_with_section(path, f"{lead}{header}\n{body}".strip()),
            meta={
                "kind": "table",
                "section_path": list(path),
                "table_key": table.key,
                "table_part": i,
                "table_parts_total": len(groups),
                "is_financial": table.is_financial,
                "boxes": (context_boxes if i == 0 else []) + boxes,
                "table": part_table.model_dump(mode="json"),
            },
        ))
    return chunks


def _chart_chunk(region: Region, path: tuple[str, ...]) -> ChunkDraft | None:
    ct = region.chart_table
    if ct is None or not (ct.header or ct.rows):
        return None
    lines = ["Chart data (read from the chart image; not in the PDF text)"]
    if ct.header:
        lines.append(" | ".join(ct.header))
    lines += [" | ".join(r) for r in ct.rows]
    check = region.check.status if region.check else None
    return ChunkDraft(
        kind="chart",
        page_start=region.page,
        page_end=region.page,
        section=_path_text(path),
        text=_with_section(path, "\n".join(lines)),
        meta={
            "kind": "chart",
            "section_path": list(path),
            "image_read": True,
            "read_by": region.read_by,
            "check_status": check,
            "boxes": [_box(region.page, region.bbox_pdf_pt, f"region:{region.page}:{region.id}")],
            "chart_table": ct.model_dump(mode="json"),
        },
    )


class _SectionTree:
    def __init__(self) -> None:
        self.levels: dict[int, str] = {}

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.levels[k] for k in sorted(self.levels))

    def enter(self, level: int, heading: str) -> None:
        self.levels = {k: v for k, v in self.levels.items() if k < level}
        self.levels[level] = heading


def build_chunks(structure: Structure) -> ChunkResult:
    tables = {(t.page, t.table_id): t for t in structure.tables}
    regions = {(r.page, r.id): r for r in structure.regions}
    joined_at: dict[tuple[int, int], tuple[int, JoinedTable]] = {}
    for n, j in enumerate(structure.joined_tables):
        for part in j.parts:
            joined_at[(part.page, part.table_id)] = (n, j)
    comma = decimal_comma(c.text or "" for t in structure.tables for c in t.cells)

    chunks: list[ChunkDraft] = []
    norm_tables: list[NormTable] = []
    warnings: list[str] = []
    buf = _TextBuffer()
    tree = _SectionTree()
    recent: list[str] = []  # texts on the current page since the last table, newest last
    page_now = 0
    page_caption: str | None = None  # unit caption of the last table on this page
    done_joined: set[int] = set()

    def flush() -> None:
        if (c := buf.flush()) is not None:
            chunks.append(c)

    for page, kind, ident in _ordered(structure):
        if page != page_now:
            page_now, recent, page_caption = page, [], None
        if kind == "table":
            t = tables.get((page, ident))
            if t is None:
                warnings.append(f"page {page}: reading order names table {ident}, not in tables")
                continue
            context = list(reversed(recent[-CONTEXT_REGIONS:]))
            if (page, ident) in joined_at:
                n, j = joined_at[(page, ident)]
                if n in done_joined:
                    continue
                done_joined.add(n)
                parts = [
                    TablePartRef(page=p.page, table_id=p.table_id,
                                 bbox=tables[(p.page, p.table_id)].bbox_pdf_pt
                                 if (p.page, p.table_id) in tables else None)
                    for p in j.parts
                ]
                source: Table | JoinedTable = j.model_copy(
                    update={"title": j.title or t.title, "subtitle": j.subtitle or t.subtitle})
                norm = normalize_table(source, context=context, previous_caption=page_caption,
                                       comma=comma, parts=parts)
            else:
                norm = normalize_table(t, context=context, previous_caption=page_caption,
                                       comma=comma)
            page_caption = norm.unit_caption or page_caption
            if not t.complete:
                norm.warnings.append("TableLens marked this table incomplete")
            if not any(c.bbox for r in norm.rows for c in r.cells) and norm.rows:
                norm.warnings.append("no cell boxes (scanned table read from its image)")
            norm_tables.append(norm)
            # a short lead-in in the same section becomes the table's context
            if buf and buf.size <= TABLE_CONTEXT_CHARS and buf.path == tree.path \
                    and not buf.subsections:
                lead, lead_boxes, lead_pages = buf.take()
            else:
                flush()
                lead, lead_boxes, lead_pages = [], [], []
            chunks.extend(_table_chunks(norm, tree.path, lead, lead_boxes, lead_pages))
            recent = []
            continue

        r = regions.get((page, ident))
        if r is None:
            warnings.append(f"page {page}: reading order names region {ident}, not in regions")
            continue
        if r.label in RUNNING:
            continue
        if r.label == "chart":
            flush()
            if (c := _chart_chunk(r, tree.path)) is not None:
                chunks.append(c)
            continue
        text = _clean(r.text)
        if not text:
            continue
        box = _box(page, r.bbox_pdf_pt, f"region:{page}:{r.id}")
        if r.label in HEADINGS and (level := heading_level(text)) is not None:
            tree.enter(level, text)
            parent = tree.path[:-1]
            # a tiny (sub)section followed by a sibling subsection under the same parent:
            # keep filling one chunk, with the parent as its section
            if (buf and level == LEAF and parent and buf.size < MIN_CHARS
                    and buf.path[: len(parent)] == parent and len(buf.path) <= len(parent) + 1):
                buf.add_subheading(text, parent)
            else:
                flush()
            recent.append(text)
            continue
        # paragraphs, list items, captions, form fields, pictures with text, and sentence-like
        # "headings" (bold leads) are text
        for piece in _split_long(text, MAX_CHARS):
            if buf and buf.size + len(piece) > MAX_CHARS:
                flush()
            buf.add(piece, page, box, tree.path)
        recent.append(text)
    flush()

    for c in chunks:
        c.meta["chunking_version"] = CHUNKING_VERSION
        if c.kind == "table" and c.meta["table"]["warnings"]:
            c.meta["warnings"] = c.meta["table"]["warnings"]
    warnings += sorted({f"{t.key}: {w}" for t in norm_tables for w in t.warnings})
    dropped = sum(c.bbox_dropped for t in [*structure.tables, *structure.joined_tables]
                  for c in t.cells)
    if dropped:
        warnings.append(f"{dropped} cell box(es) were invalid (x1 < x0 or y1 < y0) and dropped")
    return ChunkResult(chunks=chunks, tables=norm_tables, warnings=warnings)
