"""D-035: an upload must be the type the user chose, and an ESG question needs an ESG source.

* Upload: an annual report chosen as "ESG report" (or the reverse) is refused with the reason;
  nothing is stored. A scanned PDF (no text) cannot be checked and is accepted; "Other" is not checked.
* Question: an ESG question about a company with only annual reports (no BRSR section) stops
  before any model call and says what to upload; an ESG report, a BRSR or an annual report with
  a BRSR section is an ESG source.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aeids import doctype
from aeids.db.models import DocumentChunk, DocumentVersion
from tests.factories import pdf_bytes, text_pdf
from tests.test_agents_m4 import Env, _ask, env  # noqa: F401  (env is a fixture)

TEN_K = ["UNITED STATES SECURITIES AND EXCHANGE COMMISSION\nFORM 10-K\nANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d)",
         "Item 8. Financial Statements\nConsolidated Balance Sheets\nReport of Independent Registered Public Accounting Firm"]
ESG = ["Acme Corp\nSustainability Report 2024", "Scope 1 and Scope 2 emissions\nGreenhouse gas inventory\nGRI 305-1"]
BRSR = ["Business Responsibility and Sustainability Report\nFY 2024-25",
        "SECTION A: GENERAL DISCLOSURES\nPrinciple 6\nEssential Indicators\nLeadership Indicators"]
INDIAN_ANNUAL = ["Infosys Limited\nAnnual Report 2024-25",
                 "Contents\nBoard's report 98\nBusiness Responsibility and Sustainability Report 145\nStandalone financial statements 210"]
UNTITLED_FINANCIALS = ["Item 8\nConsolidated Statements of Operations",
                       "Notes to Consolidated Financial Statements\nReport of Independent Registered Public Accounting Firm"]


def _classify(pages: list[str]) -> doctype.Guess:
    path = Path(tempfile.mkstemp(suffix=".pdf")[1])
    try:
        path.write_bytes(text_pdf(pages))
        return doctype.classify_pdf(path)
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.parametrize("pages, kind", [(TEN_K, "annual_report"), (ESG, "esg_report"), (BRSR, "brsr"),
                                         (INDIAN_ANNUAL, "annual_report"), (UNTITLED_FINANCIALS, "annual_report")])
def test_the_pdf_says_what_it_is(pages: list[str], kind: str) -> None:
    assert _classify(pages).kind == kind


def test_a_scanned_pdf_cannot_be_told() -> None:
    path = Path(tempfile.mkstemp(suffix=".pdf")[1])
    path.write_bytes(pdf_bytes(3, "scan"))
    try:
        assert doctype.classify_pdf(path).kind == "unknown"
    finally:
        path.unlink(missing_ok=True)


def _upload(client: TestClient, pdf: bytes, document_type: str, company: str = "Acme Corp"):
    return client.post("/api/v1/documents", data={"company_name": company, "document_type": document_type},
                       files={"file": ("report.pdf", pdf, "application/pdf")})


@pytest.mark.parametrize("pages, chosen, said", [
    (TEN_K, "esg_report", 'page 1 says "Form 10-K"'),
    (TEN_K, "brsr", 'page 1 says "Form 10-K"'),
    (ESG, "annual_report", "Sustainability / ESG Report"),
    (BRSR, "annual_report", "Business Responsibility and Sustainability Report"),
])
def test_an_upload_of_the_wrong_type_is_refused_and_nothing_is_stored(client: TestClient, pages, chosen, said) -> None:
    resp = _upload(client, text_pdf(pages, chosen), chosen)
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "document_type_mismatch" and said in err["message"]
    assert client.get("/api/v1/documents").json() == []


def test_an_annual_report_with_its_brsr_is_an_annual_report(client: TestClient) -> None:
    resp = _upload(client, text_pdf(INDIAN_ANNUAL, "brsr"), "brsr")
    assert resp.status_code == 422 and "upload it as an Annual report" in resp.json()["error"]["message"]
    assert _upload(client, text_pdf(INDIAN_ANNUAL, "annual"), "annual_report").status_code == 201


@pytest.mark.parametrize("pages, chosen", [(TEN_K, "annual_report"), (ESG, "esg_report"), (ESG, "brsr"),
                                           (BRSR, "brsr"), (BRSR, "esg_report"), (TEN_K, "other")])
def test_the_right_type_is_accepted(client: TestClient, pages, chosen) -> None:
    assert _upload(client, text_pdf(pages, chosen), chosen).status_code == 201


def test_a_scanned_pdf_is_accepted_as_any_type(client: TestClient) -> None:
    assert _upload(client, pdf_bytes(2, "scan"), "esg_report").status_code == 201


def test_a_new_version_must_be_the_same_kind_of_report(client: TestClient) -> None:
    doc = _upload(client, text_pdf(TEN_K, "v1"), "annual_report").json()["document"]
    resp = client.post(f"/api/v1/documents/{doc['id']}/versions",
                       files={"file": ("esg.pdf", text_pdf(ESG, "v2"), "application/pdf")})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "document_type_mismatch"


@pytest.mark.parametrize("question, esg", [
    ("What were Apple's Scope 1 emissions in 2024?", True), ("What was Infosys's energy intensity in FY2025?", True),
    ("What is TCS's water consumption?", True), ("What was the LTIFR at Tata Steel?", True),
    ("Summarise Apple's BRSR disclosures", True), ("How much energy storage did Tesla deploy in 2025?", False),
    ("How many employees did Microsoft have?", False), ("How many women are on Apple's board?", False),
    ("What was Apple's operating margin in fiscal 2025?", False)])
def test_which_questions_need_an_esg_report(question: str, esg: bool) -> None:
    assert doctype.needs_esg_report(question) is esg


# ---------------- questions ----------------

def test_an_esg_question_on_an_annual_report_is_not_answered(env: Env) -> None:
    wf = _ask(env, "What were Apple's Scope 1 emissions in 2024?")
    assert wf["status"] == "NEEDS_HUMAN_REVIEW"
    extract = wf["tasks_by_key"]["extract"]
    assert "only annual reports" in extract["last_error"] and "Apple Inc." in extract["last_error"]
    assert "upload the company's ESG / sustainability report or BRSR" in extract["last_error"]
    assert env.llm.prompts == []  # stopped before any model call
    assert wf["findings"] == []


def test_an_annual_report_with_a_brsr_section_answers_esg_questions(env: Env, session) -> None:
    version = session.scalar(select(DocumentVersion).where(DocumentVersion.is_current.is_(True)))
    chunk = session.scalar(select(DocumentChunk).where(DocumentChunk.version_id == version.id).limit(1))
    chunk.section = "Business Responsibility and Sustainability Report > Principle 6 > Essential Indicators"
    session.commit()
    wf = _ask(env, "What were Apple's Scope 1 emissions in 2024?")
    assert "only annual reports" not in (wf["tasks_by_key"]["extract"]["last_error"] or "")
    assert env.llm.prompts  # it went on to plan


def test_an_esg_report_answers_esg_questions(env: Env) -> None:
    resp = env.client.post("/api/v1/documents", data={"company_name": "Apple Inc.", "document_type": "esg_report"},
                           files={"file": ("apple_esg.pdf", pdf_bytes(2, "esg"), "application/pdf")})
    assert resp.status_code == 201, resp.text
    wf = _ask(env, "What were Apple's Scope 1 emissions in 2024?")
    assert "only annual reports" not in (wf["tasks_by_key"]["extract"]["last_error"] or "")


def test_financial_questions_are_not_affected(env: Env) -> None:
    wf = _ask(env, "Compare Apple's revenue and operating margin FY2023–FY2025")
    assert wf["status"] == "COMPLETED"

