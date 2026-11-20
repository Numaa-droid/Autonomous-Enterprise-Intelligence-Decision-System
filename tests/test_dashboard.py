"""Dashboard API (/api/v1/dashboard) and the /ui page."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr
from fastapi.testclient import TestClient

from aeids.api.routers import dashboard
from tests.factories import pdf_bytes
from tests.test_search import report
from tests.test_tablelens import FakeTableLens, _client


@pytest.fixture
def indexed(client: TestClient) -> dict:
    state = client.app.state
    state.settings = state.settings.model_copy(update={"jobs_mode": "inline"})
    state.tablelens = _client(FakeTableLens(report()))
    resp = client.post("/api/v1/documents", data={"company_name": "Apple Inc.", "fiscal_year": "2025"},
                       files={"file": ("NASDAQ_AAPL_2025.pdf", pdf_bytes(2, "d"), "application/pdf")})
    assert resp.json()["version"]["parser_status"] == "indexed"
    return resp.json()


def test_ui_is_served(client: TestClient) -> None:
    assert client.get("/", follow_redirects=False).headers["location"] == "/ui/"
    page = client.get("/ui/")
    assert page.status_code == 200 and "<title>" in page.text


def test_overview_without_data(client: TestClient) -> None:
    out = client.get("/api/v1/dashboard/overview").json()
    assert out["documents"]["documents"] == 0 and len(out["activity"]) == 14
    assert client.get("/api/v1/dashboard/overview?tenant=nobody").status_code == 404


def test_overview_documents_and_traces(client: TestClient, indexed: dict) -> None:
    out = client.get("/api/v1/dashboard/overview").json()
    d = out["documents"]
    assert d["documents"] == 1 and d["by_status"] == {"indexed": 1}
    assert d["chunks"] == 3 and d["chunk_kinds"] == {"text": 2, "table": 1}
    assert d["indexed_points"] == 3
    assert out["totals_7d"]["traces"] == 1 and out["activity"][-1]["traces"] == 1
    assert {row["kind"] for row in out["latency"]} >= {"job", "index", "vectordb"}
    assert out["recent_traces"][0]["name"] == "process_version"

    docs = client.get("/api/v1/dashboard/documents").json()
    assert docs[0]["company"] == "Apple Inc." and docs[0]["versions"][0]["chunks"] == 3
    assert client.get("/api/v1/dashboard/tenants").json()["tenants"] == ["default"]


def test_traces_list_and_detail(client: TestClient, indexed: dict) -> None:
    listed = client.get("/api/v1/dashboard/traces").json()
    assert listed["total"] == 1
    row = listed["traces"][0]
    assert row["name"] == "process_version" and row["status"] == "ok" and row["spans"] >= 7
    assert row["preview"].startswith("version_id") or row["preview"].startswith("filename")
    detail = client.get(f"/api/v1/dashboard/traces/{row['trace_id']}").json()
    spans = detail["spans"]
    assert spans[0]["parent_span_id"] is None and spans[0]["offset_ms"] == 0
    assert all(s["offset_ms"] >= 0 for s in spans)
    assert client.get("/api/v1/dashboard/traces/../../etc").status_code == 404
    assert client.get("/api/v1/dashboard/traces/" + "0" * 32).status_code == 404
    assert client.get("/api/v1/dashboard/traces?status=error").json()["traces"] == []


def test_playground_search_links_its_trace(client: TestClient, indexed: dict) -> None:
    out = client.post("/api/v1/dashboard/search", json={"query": "supply chain disruptions"}).json()
    assert out["hits"][0]["section"] == "Item 1A. Risk Factors"
    spans = client.get(f"/api/v1/dashboard/traces/{out['trace_id']}").json()["spans"]
    assert [s["name"] for s in spans][:2] == ["dashboard.search", "search"]
    assert {"dense_search", "keyword_search", "rrf_fuse"} <= {s["name"] for s in spans}
    resp = client.post("/api/v1/dashboard/search", json={"query": "x", "tenant": "nobody"})
    assert resp.status_code == 404


def test_evals(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "REPORTS", tmp_path)
    run = {"created_at": "2026-11-20T17:20:52Z", "embedding_model": "m", "corpus": ["AAPL"],
           "scores": {"hybrid": {"recall@5": 0.8}}, "questions": [{"id": "a1"}]}
    (tmp_path / "retrieval-20261120T172052Z.json").write_text(json.dumps(run))
    (tmp_path / "notes.json").write_text("{}")
    listed = client.get("/api/v1/dashboard/evals").json()
    assert [r["name"] for r in listed] == ["retrieval-20261120T172052Z.json"]
    assert listed[0]["questions"] == 1
    assert client.get("/api/v1/dashboard/evals/retrieval-20261120T172052Z.json").json() == run
    assert client.get("/api/v1/dashboard/evals/notes.json").status_code == 404


def test_logs(client: TestClient, tmp_path: Path) -> None:
    state = client.app.state
    state.settings = state.settings.model_copy(update={"log_dir": tmp_path})
    assert client.get("/api/v1/dashboard/logs").json()["hint"]
    (tmp_path / "worker.log").write_text(
        "2026-11-20T10:00:01.000+00:00 INFO rq.worker: listening\n"
        "2026-11-20T10:00:03.000+00:00 WARNING aeids: slow\n")
    (tmp_path / "api.log").write_text("2026-11-20T10:00:02.000+00:00 ERROR boom\n")
    out = client.get("/api/v1/dashboard/logs").json()
    assert [e["level"] for e in out["entries"]] == ["INFO", "ERROR", "WARNING"]  # by time
    assert set(out["sources"]) == {"worker", "api"}
    only = client.get("/api/v1/dashboard/logs?source=worker&q=slow").json()["entries"]
    assert [e["text"] for e in only] == ["aeids: slow"]  # the level is a field, not repeated
    assert client.get("/api/v1/dashboard/logs?source=../etc/passwd").status_code == 422


def test_dashboard_polling_is_not_logged() -> None:
    import logging

    quiet = dashboard._QuietDashboard()
    rec = lambda method, path: logging.LogRecord("uvicorn.access", 20, "", 0, '%s - "%s %s"', ("1.2.3.4", method, path, "1.1", 200), None)
    assert not quiet.filter(rec("GET", "/api/v1/dashboard/overview"))
    assert not quiet.filter(rec("GET", "/ui/app.js"))
    assert quiet.filter(rec("POST", "/api/v1/dashboard/search"))
    assert quiet.filter(rec("GET", "/api/v1/documents"))


def test_services_never_show_secrets(client: TestClient) -> None:
    state = client.app.state
    state.settings = state.settings.model_copy(update={
        "openrouter_api_key": SecretStr("sk-or-v1-secret-value"), "tablelens_url": "http://127.0.0.1:9"})
    out = client.get("/api/v1/dashboard/services")
    assert out.status_code == 200 and "secret" not in out.text
    body = out.json()
    assert body["openrouter"] == {"ok": True, "detail": "API key set"}
    assert body["tablelens"]["ok"] is False and body["database"]["ok"] is True


def test_overview_before_the_first_upload(client: TestClient) -> None:
    """The default tenant only exists after the first upload; the console still opens."""
    assert client.get("/api/v1/dashboard/overview?tenant=default").status_code == 200
    assert client.get("/api/v1/dashboard/documents?tenant=default").status_code == 404


def test_times_are_sent_with_a_timezone(client: TestClient, indexed: dict) -> None:
    v = client.get("/api/v1/dashboard/documents").json()[0]["versions"][0]
    for key in ("uploaded_at", "parsed_at", "indexed_at"):
        assert v[key].endswith(("+00:00", "Z")), (key, v[key])  # SQLite stores them naive
