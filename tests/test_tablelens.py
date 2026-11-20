"""TableLens client and automatic processing, against a fake TableLens server (no GPU, no network)."""

from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from aeids.api import processing
from aeids.ingestion.tablelens import (
    TableLensClient,
    TableLensJobFailed,
    TableLensTimeout,
    TableLensUnavailable,
    tablelens_stem,
)
from aeids.jobs import JobQueueUnavailable
from tests.factories import INCOME_ROWS, INCOME_SUBTITLE, dumps, grid_table, pdf_bytes, region, structure


def _structure_bytes(pages: int = 2) -> bytes:
    tables = [grid_table(pages, 1, INCOME_ROWS, header_rows=2, order=2,
                         title="CONSOLIDATED STATEMENTS OF OPERATIONS", subtitle=INCOME_SUBTITLE)]
    regions = [region(1, 1, "heading", "Item 8. Financial Statements", order=1),
               region(1, 2, "paragraph", "The Company designs smartphones. " * 20, order=2)]
    return dumps(structure(tables, regions))


class FakeTableLens:
    """Enough of tablelens/app.py: /health, /detect, /recognise, /events, /output."""

    def __init__(self, structure_bytes: bytes | None = None, *, fail: str | None = None,
                 polls_per_job: int = 2) -> None:
        self.structure = structure_bytes or _structure_bytes()
        self.fail = fail  # "detect-error", "recognise-400", "events-404", "down"
        self.polls_per_job = polls_per_job
        self.outputs: dict[str, bytes] = {}
        self.jobs: dict[str, dict] = {}
        self.calls: list[str] = []
        self.detected: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if self.fail == "down":
            raise httpx.ConnectError("connection refused")
        if path == "/health":
            return httpx.Response(200, json={"ok": True, "vlm": False, "gpu_server": False,
                                             "jobs": 0})
        if path == "/detect":
            name = re.search(rb'filename="([^"]+)"', request.content).group(1).decode()
            self.detected.append(name)
            events = [{"type": "meta", "file": name, "pages": 2},
                      {"type": "page", "page": 1, "image": "data:image/jpeg;base64,AAAA"},
                      {"type": "log", "level": "warn", "msg": "a warning from detection"}]
            if self.fail == "detect-error":
                events.append({"type": "error", "detail": "RuntimeError: CUDA out of memory"})
            else:
                events.append({"type": "summary", "tables": 1})
            return self._job(events)
        if path == "/recognise":
            if self.fail == "recognise-400":
                return httpx.Response(400, json={"detail": "mode is 'tables' or 'all'."})
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            stem = form["name"]
            assert form["mode"] == "all"
            self.outputs[stem] = self.structure
            return self._job([{"type": "rmeta", "total": 1},
                              {"type": "log", "level": "err",
                               "msg": "Reference JSON not written: KeyError: 'patient'"},
                              {"type": "rsummary", "tables": 1}])
        if path.startswith("/events/"):
            if self.fail == "events-404":
                return httpx.Response(404, json={"detail": "Unknown or expired job."})
            job = self.jobs[path.rsplit("/", 1)[1]]
            since = int(request.url.params.get("since", 0))
            job["polls"] += 1
            done = job["polls"] >= self.polls_per_job
            out = job["events"][since:] if done else job["events"][since:since + 1]
            return httpx.Response(200, json={"events": out, "next": since + len(out),
                                             "done": done and since + len(out) >= len(job["events"])})
        if m := re.fullmatch(r"/output/([^/]+)/structure/([^/]+)_structure\.json", path):
            data = self.outputs.get(m.group(1))
            return httpx.Response(200, content=data) if data else httpx.Response(404)
        return httpx.Response(404)

    def _job(self, events: list[dict]) -> httpx.Response:
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"events": events, "polls": 0}
        return httpx.Response(200, json={"job": job_id})


def _client(fake: FakeTableLens, **kw) -> TableLensClient:
    return TableLensClient("http://tablelens.test", transport=httpx.MockTransport(fake),
                           sleep=lambda _s: None, **kw)


# ---------- client ----------

def test_stem_is_tablelens_safe_and_unique_per_file() -> None:
    assert tablelens_stem("../NASDAQ AAPL 2025.pdf", "3eb270b22acb7d8d" + "0" * 48) == \
        "NASDAQ_AAPL_2025-3eb270b22acb"
    assert tablelens_stem("x.pdf", "a" * 64) != tablelens_stem("x.pdf", "b" * 64)


def test_parse_runs_detect_then_recognise_and_downloads(tmp_path) -> None:
    fake = FakeTableLens()
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(pdf_bytes(2))
    seen: list[str] = []
    data, warnings = _client(fake).parse(pdf, "report-abc", on_event=lambda e: seen.append(e["type"]))
    assert data == fake.structure
    assert fake.detected == ["report-abc.pdf"]
    assert fake.calls[0] == "POST /detect" and "POST /recognise" in fake.calls
    assert "page" not in seen  # big image events are not passed on
    assert "summary" in seen and "rsummary" in seen
    assert "a warning from detection" in warnings


def test_job_error_event_fails(tmp_path) -> None:
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(pdf_bytes(1))
    with pytest.raises(TableLensJobFailed, match="CUDA out of memory"):
        _client(FakeTableLens(fail="detect-error")).parse(pdf, "r")


@pytest.mark.parametrize(("fail", "error", "retryable"), [
    ("down", TableLensUnavailable, True),
    ("events-404", TableLensUnavailable, True),
    ("recognise-400", TableLensJobFailed, False),
])
def test_failures_are_classified(tmp_path, fail, error, retryable) -> None:
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(pdf_bytes(1))
    with pytest.raises(error) as info:
        _client(FakeTableLens(fail=fail)).parse(pdf, "r")
    assert info.value.retryable is retryable


def test_job_timeout(tmp_path) -> None:
    ticks = iter(range(0, 10_000, 100))
    fake = FakeTableLens(polls_per_job=1000)
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(pdf_bytes(1))
    client = _client(fake, job_timeout_seconds=250, clock=lambda: next(ticks))
    with pytest.raises(TableLensTimeout):
        client.parse(pdf, "r")


def test_poll_interval_is_capped_below_tablelens_idle_limit() -> None:
    assert _client(FakeTableLens(), poll_seconds=120).poll_seconds <= 10


# ---------- automatic processing through the API ----------

@pytest.fixture
def inline(client: TestClient):
    fake = FakeTableLens()
    state = client.app.state
    state.settings = state.settings.model_copy(update={"jobs_mode": "inline"})
    state.tablelens = _client(fake)
    return fake


def _upload(client: TestClient, pages: int = 2, tag: str = "a", name: str = "NASDAQ_AAPL_2025.pdf"):
    return client.post("/api/v1/documents",
                       files={"file": (name, pdf_bytes(pages, tag), "application/pdf")},
                       data={"company_name": "Apple Inc."})


def test_upload_is_parsed_by_tablelens(client: TestClient, inline: FakeTableLens) -> None:
    resp = _upload(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["processing"] == "done"
    assert body["version"]["parser_status"] == "indexed"  # parsed, then indexed for search
    assert body["version"]["is_current"] is True
    assert body["version"]["index_point_count"] == 2
    checksum = body["version"]["checksum"]
    assert inline.detected == [f"NASDAQ_AAPL_2025-{checksum[:12]}.pdf"]
    warnings = body["version"]["parse_warnings"]
    assert "TableLens: a warning from detection" in warnings
    assert not any("Reference JSON" in w for w in warnings)  # medical step noise dropped
    doc, ver = body["document"]["id"], body["version"]["id"]
    chunks = client.get(f"/api/v1/documents/{doc}/versions/{ver}/chunks").json()
    assert [c["kind"] for c in chunks] == ["text", "table"]


def test_duplicate_upload_is_not_parsed_again(client: TestClient, inline: FakeTableLens) -> None:
    _upload(client)
    second = _upload(client).json()
    assert second["processing"] == "not_needed"
    assert len(inline.detected) == 1


def test_existing_tablelens_result_is_reused(client: TestClient, inline: FakeTableLens) -> None:
    import hashlib
    checksum = hashlib.sha256(pdf_bytes(2, "a")).hexdigest()
    inline.outputs[tablelens_stem("NASDAQ_AAPL_2025.pdf", checksum)] = inline.structure
    body = _upload(client).json()
    assert body["version"]["parser_status"] == "indexed"
    assert inline.detected == []  # TableLens already had the result for this file
    assert any("reused" in w for w in body["version"]["parse_warnings"])


def test_reuse_by_filename_uses_an_earlier_tablelens_run(client: TestClient) -> None:
    state = client.app.state
    fake = FakeTableLens()
    fake.outputs["NASDAQ_AAPL_2025"] = fake.structure  # the user's earlier manual run
    state.settings = state.settings.model_copy(update={"jobs_mode": "off"})
    state.tablelens = _client(fake)
    body = _upload(client).json()
    assert body["processing"] == "off" and body["version"]["parser_status"] == "pending"
    state.settings = state.settings.model_copy(update={"jobs_mode": "inline"})
    doc, ver = body["document"]["id"], body["version"]["id"]
    resp = client.post(f"/api/v1/documents/{doc}/versions/{ver}/process?reuse_by_filename=true")
    assert resp.json()["version"]["parser_status"] == "indexed"
    assert fake.detected == []
    assert any("matched by file name" in w for w in resp.json()["version"]["parse_warnings"])


def test_tablelens_failure_marks_failed_then_retry_works(client: TestClient) -> None:
    state = client.app.state
    state.settings = state.settings.model_copy(update={"jobs_mode": "inline"})
    state.tablelens = _client(FakeTableLens(fail="detect-error"))
    body = _upload(client).json()
    assert body["processing"] == "failed"
    assert body["version"]["parser_status"] == "failed"
    assert "CUDA out of memory" in body["version"]["parser_error"]

    state.tablelens = _client(FakeTableLens())  # TableLens fixed
    doc, ver = body["document"]["id"], body["version"]["id"]
    resp = client.post(f"/api/v1/documents/{doc}/versions/{ver}/process")
    assert resp.json()["version"]["parser_status"] == "indexed"


def test_wrong_structure_from_tablelens_fails_the_version(client: TestClient) -> None:
    state = client.app.state
    state.settings = state.settings.model_copy(update={"jobs_mode": "inline"})
    state.tablelens = _client(FakeTableLens(_structure_bytes(pages=9)))  # 9 pages, PDF has 2
    body = _upload(client).json()
    assert body["version"]["parser_status"] == "failed"
    assert "page 9" in body["version"]["parser_error"]


def test_queue_mode_enqueues_once(client: TestClient, monkeypatch) -> None:
    state = client.app.state
    state.settings = state.settings.model_copy(update={"jobs_mode": "queue"})
    calls = []

    def fake_enqueue(settings, tenant_id, version_id, **kw):
        calls.append(version_id)
        return f"process-{version_id}", len(calls) == 1

    monkeypatch.setattr(processing, "enqueue_process_version", fake_enqueue)
    body = _upload(client).json()
    assert body["processing"] == "queued"
    assert body["job_id"] == f"process-{body['version']['id']}"
    assert body["version"]["parser_status"] == "pending"  # the worker will parse it
    doc, ver = body["document"]["id"], body["version"]["id"]
    again = client.post(f"/api/v1/documents/{doc}/versions/{ver}/process").json()
    assert again["processing"] == "already_queued"


def test_queue_down_keeps_the_upload(client: TestClient, monkeypatch) -> None:
    state = client.app.state
    state.settings = state.settings.model_copy(update={"jobs_mode": "queue"})

    def down(*_a, **_kw):
        raise JobQueueUnavailable("job queue unavailable: ConnectionError")

    monkeypatch.setattr(processing, "enqueue_process_version", down)
    resp = _upload(client)
    assert resp.status_code == 201
    assert resp.json()["processing"] == "not_queued"


def test_indexed_version_cannot_be_processed_again(client: TestClient, inline, session) -> None:
    from aeids.db.models import DocumentVersion
    body = _upload(client).json()
    v = session.get(DocumentVersion, uuid.UUID(body["version"]["id"]))
    v.parser_status = "indexed"
    session.commit()
    resp = client.post(f"/api/v1/documents/{body['document']['id']}/versions/{v.id}/process")
    assert resp.status_code == 409


def test_structure_bytes_are_valid_json() -> None:
    assert json.loads(_structure_bytes())["tables"]
