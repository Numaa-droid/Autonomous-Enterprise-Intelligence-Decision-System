"""Is a report the kind the user says it is, and can a question be answered from the kinds uploaded? (D-035)

Two checks keep a wrong document type from turning into a wrong answer:

* **On upload** (:func:`classify_pdf`, used by ``documents.check_document_type``): AEIDS reads
  the text of the first pages and a sample of the rest and decides whether the PDF is an annual
  report, an ESG / sustainability report or a BRSR. The title decides first (whichever of
  "Form 10-K" / "Annual Report", "Sustainability Report" / "ESG Report" or "Business
  Responsibility and Sustainability Report" comes first); otherwise the content does (financial
  statements and auditor's reports, or Scope 1/2 emissions, GRI and TCFD, or BRSR's sections and
  principles). An upload whose chosen type is of the other family is refused with the reason.
  A scanned PDF without a text layer cannot be read here and is accepted; "Other" is not checked.
* **On a question** (:func:`needs_esg_report`, used by Document Intelligence): a question about
  emissions, energy use, water, waste, workplace injuries, BRSR or sustainability needs an ESG
  source. A company has one when one of its current reports is an ESG report or a BRSR, or an
  annual report that contains a BRSR / sustainability section (common in India). Headcount, board
  members or "energy storage" are not ESG-only: annual reports disclose them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ESG_FAMILY = {"esg_report", "brsr"}
LABEL = {"annual_report": "an annual report", "esg_report": "an ESG / sustainability report", "brsr": "a BRSR"}
TYPE_NAME = {"annual_report": "Annual report", "esg_report": "ESG report", "brsr": "BRSR"}

TITLE_PAGES = 5  # the cover and contents
SAMPLE_PAGES = 10  # spread over the rest of the document

_TITLES: list[tuple[str, re.Pattern[str], str]] = [
    ("brsr", re.compile(r"business\s+responsibility\s+(?:and|&)\s+sustainability\s+report"),
     "Business Responsibility and Sustainability Report"),
    ("annual_report", re.compile(r"\bform\s+10-k\b"), "Form 10-K"),
    ("annual_report", re.compile(r"\bform\s+20-f\b"), "Form 20-F"),
    ("annual_report", re.compile(r"\bannual\s+report\b"), "Annual Report"),
    ("esg_report", re.compile(r"\b(?:sustainability|esg|impact|climate|corporate\s+(?:social\s+)?responsibility)\s+report\b"),
     "Sustainability / ESG Report"),
    ("esg_report", re.compile(r"\benvironmental,?\s+social,?\s+(?:and|&)\s+governance\s+report\b"), "ESG Report"),
]
_BODY: dict[str, list[re.Pattern[str]]] = {
    "annual_report": [re.compile(p) for p in (
        r"consolidated\s+balance\s+sheets?", r"statements?\s+of\s+profit\s+and\s+loss",
        r"consolidated\s+statements?\s+of\s+(?:operations|income|cash\s+flows|comprehensive\s+income)",
        r"report\s+of\s+independent\s+registered\s+public\s+accounting\s+firm", r"independent\s+auditor'?s'?\s+report",
        r"management'?s\s+discussion\s+and\s+analysis", r"directors'?\s+report",
        r"notes\s+to\s+(?:the\s+)?(?:consolidated\s+)?financial\s+statements")],
    "esg_report": [re.compile(p) for p in (
        r"\bscope\s*1\b", r"\bscope\s*2\b", r"greenhouse\s+gas", r"\bgri\s*\d", r"\btcfd\b", r"\bsasb\b",
        r"water\s+withdraw", r"materiality\s+assessment", r"renewable\s+(?:energy|electricity)")],
    "brsr": [re.compile(p) for p in (
        r"section\s+a\s*[:.\-]?\s*general\s+disclosures", r"\bprinciple\s+[1-9]\b", r"essential\s+indicators",
        r"leadership\s+indicators")],
}

# a question about these needs an ESG source (see the module docstring)
_ESG_QUESTION = re.compile(
    r"\b(esg|brsr|sustainab\w*|scope\s*[123]|ghg|greenhouse|(carbon|co2|ghg)\s+emissions?|emissions?(\s+intensity)?|"
    r"carbon\s+(footprint|neutral\w*)|net[- ]zero|energy\s+(consumption|consumed|intensity|use|usage|efficiency)|"
    r"renewable\s+(energy|electricity|power)|water\s+(withdrawal|withdrawn|consumption|consumed|intensity|usage)|"
    r"waste\s+(generated|recycl\w*|intensity|management)|ltifr|lost[- ]time\s+injur\w*|injury\s+(rate|frequency)|"
    r"csr\s+(spend|spent|expenditure|obligation))\b", re.I)
# a section heading that makes an annual report an ESG source too (SQL LIKE patterns, lower case)
ESG_SECTION_PATTERNS = ("%business responsibility%", "%sustainability report%", "%brsr%", "%esg report%")


def needs_esg_report(question: str) -> bool:
    return bool(_ESG_QUESTION.search(question or ""))


@dataclass(frozen=True)
class Guess:
    kind: str  # annual_report | esg_report | brsr | unknown
    reason: str  # e.g. 'page 1 says "Form 10-K"'
    text_pages: int  # pages with readable text among those read

    @property
    def family(self) -> str | None:
        if self.kind == "annual_report":
            return "annual"
        return "esg" if self.kind in ESG_FAMILY else None


def _pages_to_read(n: int) -> list[int]:
    first = list(range(min(n, TITLE_PAGES)))
    rest = n - len(first)
    if rest <= 0:
        return first
    step = max(1, rest // SAMPLE_PAGES)
    return first + list(range(len(first), n, step))[:SAMPLE_PAGES]


def _read(path: Path) -> list[tuple[int, str]]:
    from pypdf import PdfReader  # the same reader the ingestion page-count check uses

    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            return []
        pages = []
        for i in _pages_to_read(len(reader.pages)):
            try:
                text = reader.pages[i].extract_text() or ""
            except Exception:  # noqa: BLE001 — one unreadable page does not decide anything
                text = ""
            pages.append((i, " ".join(text.lower().split())))
        return pages
    except Exception:  # noqa: BLE001 — an unreadable PDF is "unknown"; parsing reports the real error
        return []


def classify_pdf(path: Path) -> Guess:
    pages = _read(path)
    text_pages = sum(1 for _, t in pages if len(t) >= 40)
    if text_pages == 0:
        return Guess("unknown", "no text layer to read (a scanned PDF?)", 0)
    best: tuple[int, int, str, str] | None = None  # (page, offset, kind, phrase): the title that comes first
    for i, text in pages:
        if i >= TITLE_PAGES:
            break
        for kind, pattern, phrase in _TITLES:
            m = pattern.search(text)
            if m and (best is None or (i, m.start()) < best[:2]):
                best = (i, m.start(), kind, phrase)
    if best is not None:
        return Guess(best[2], f'page {best[0] + 1} says "{best[3]}"', text_pages)
    body = " ".join(t for _, t in pages)
    scores = {kind: sum(1 for p in pats if p.search(body)) for kind, pats in _BODY.items()}
    if scores["annual_report"] >= 2 and scores["annual_report"] > scores["esg_report"] + scores["brsr"]:
        return Guess("annual_report", "it contains financial statements and an auditor's report", text_pages)
    if scores["brsr"] >= 2:
        return Guess("brsr", "it follows the BRSR format (general disclosures, principles, indicators)", text_pages)
    if scores["esg_report"] >= 2:
        return Guess("esg_report", "it reports emissions and sustainability indicators", text_pages)
    return Guess("unknown", "the type could not be recognised", text_pages)


def mismatch_message(selected: str, guess: Guess) -> str | None:
    """Why ``selected`` is the wrong type for the PDF, or None when it fits (or cannot be told)."""
    chosen = "annual" if selected == "annual_report" else "esg" if selected in ESG_FAMILY else None
    if chosen is None or guess.family is None or chosen == guess.family:
        return None
    if guess.family == "annual":
        hint = (f'Choose "{TYPE_NAME["annual_report"]}" as the document type, or upload the company\'s ESG / '
                "sustainability report or BRSR. If this annual report includes a BRSR section, upload it as an "
                "Annual report: AEIDS reads that section too, and ESG questions can use it.")
    else:
        hint = (f'Choose "{TYPE_NAME.get(guess.kind, "ESG report")}" as the document type, or upload the '
                "company's annual report.")
    return f"This PDF looks like {LABEL[guess.kind]}, not {LABEL[selected]}: {guess.reason}. {hint}"
