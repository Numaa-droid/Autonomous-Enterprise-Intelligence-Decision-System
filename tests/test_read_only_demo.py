"""D-039: AEIDS_READ_ONLY=true (public demo) refuses every change except searching and asking a
question; reads still work, and the console learns the mode from /tenants. D-040: the owner key
(AEIDS_OWNER_KEY, header X-AEIDS-Owner-Key) lets the owner change data in the demo."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine

from aeids.api.app import create_app
from aeids.api.security import READ_ONLY_ALLOWED
from aeids.config import Settings
from tests.factories import pdf_bytes


@pytest.fixture
def demo(settings: Settings, engine: Engine, vector_index, embedder) -> Iterator[TestClient]:
    app = create_app(settings.model_copy(update={"read_only": True}))
    app.state.vector_index = vector_index
    app.state.embedder = embedder
    with TestClient(app) as c:
        yield c


def _refused(resp) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "read_only"


def test_changes_are_refused(demo: TestClient) -> None:
    doc, ver, wf = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pdf = {"file": ("a.pdf", pdf_bytes(2, "x"), "application/pdf")}
    _refused(demo.post("/api/v1/documents", files=pdf, data={"company_name": "Apple Inc."}))
    _refused(demo.post(f"/api/v1/documents/{doc}/versions", files=pdf))
    _refused(demo.delete(f"/api/v1/documents/{doc}"))
    _refused(demo.post(f"/api/v1/documents/{doc}/versions/{ver}/process"))
    _refused(demo.post(f"/api/v1/documents/{doc}/versions/{ver}/structure"))
    _refused(demo.post(f"/api/v1/workflows/{wf}/reviews", json={"task_id": str(uuid.uuid4()), "decision": "approve"}))
    _refused(demo.post(f"/api/v1/workflows/{wf}/cancel"))
    _refused(demo.post(f"/api/v1/dashboard/workflows/{wf}/reviews", json={}))
    _refused(demo.put("/api/v1/search", json={}))  # only the listed method is allowed
    _refused(demo.patch("/api/v1/documents", json={}))


def test_reads_search_and_questions_still_work(demo: TestClient) -> None:
    assert demo.get("/api/v1/documents").status_code == 200
    assert demo.get("/health").status_code == 200
    resp = demo.post("/api/v1/search", json={"query": "revenue"})
    assert resp.status_code == 200, resp.text
    # a question is accepted by the guard (the endpoint itself decides the rest)
    for path in ("/api/v1/workflows", "/api/v1/workflows/", "/api/v1/dashboard/workflows"):
        assert demo.post(path, json={"question": "What was Apple's revenue?"}).status_code != 403
    assert demo.post("/api/v1/dashboard/search", json={"query": "revenue"}).status_code != 403


def test_console_learns_the_mode(demo: TestClient, client: TestClient) -> None:
    assert demo.get("/api/v1/dashboard/tenants").json()["read_only"] is True
    assert client.get("/api/v1/dashboard/tenants").json()["read_only"] is False


def test_default_is_not_read_only(client: TestClient) -> None:
    assert Settings(_env_file=None).read_only is False
    resp = client.post("/api/v1/documents", files={"file": ("a.pdf", pdf_bytes(2, "y"), "application/pdf")},
                       data={"company_name": "Apple Inc."})
    assert resp.status_code == 201, resp.text


def test_allow_list_is_only_search_and_questions() -> None:
    assert READ_ONLY_ALLOWED == {("POST", "/api/v1/search"), ("POST", "/api/v1/workflows"),
                                 ("POST", "/api/v1/dashboard/search"), ("POST", "/api/v1/dashboard/workflows")}


# --- D-040: the owner key unlocks changes for the owner only -----------------------------------

OWNER_KEY = "owner-key-for-tests-0123456789"


@pytest.fixture
def owned(settings: Settings, engine: Engine, vector_index, embedder) -> Iterator[TestClient]:
    app = create_app(settings.model_copy(update={"read_only": True, "owner_key": SecretStr(OWNER_KEY)}))
    app.state.vector_index = vector_index
    app.state.embedder = embedder
    with TestClient(app) as c:
        yield c


def _upload(c: TestClient, tag: str, headers: dict | None = None):
    return c.post("/api/v1/documents", files={"file": ("a.pdf", pdf_bytes(2, tag), "application/pdf")},
                  data={"company_name": "Apple Inc."}, headers=headers or {})


def test_owner_key_unlocks_changes(owned: TestClient) -> None:
    key = {"X-AEIDS-Owner-Key": OWNER_KEY}
    resp = _upload(owned, "o1", key)
    assert resp.status_code == 201, resp.text
    doc = resp.json()["document"]["id"]
    _refused(owned.delete(f"/api/v1/documents/{doc}"))  # visitors: still read-only
    assert owned.delete(f"/api/v1/documents/{doc}", headers=key).status_code == 204


def test_wrong_or_missing_owner_key_is_refused(owned: TestClient) -> None:
    _refused(_upload(owned, "o2"))
    _refused(_upload(owned, "o3", {"X-AEIDS-Owner-Key": "not-the-key-0123456789"}))
    _refused(_upload(owned, "o4", {"X-AEIDS-Owner-Key": ""}))


def test_console_learns_owner_mode(owned: TestClient, demo: TestClient) -> None:
    visitor = owned.get("/api/v1/dashboard/tenants").json()
    assert visitor["read_only"] is True and visitor["can_edit"] is False and visitor["owner_unlock"] is True
    owner = owned.get("/api/v1/dashboard/tenants", headers={"X-AEIDS-Owner-Key": OWNER_KEY}).json()
    assert owner["can_edit"] is True
    # no owner key configured: nothing to unlock, and any header is ignored
    assert demo.get("/api/v1/dashboard/tenants").json()["owner_unlock"] is False
    _refused(_upload(demo, "o5", {"X-AEIDS-Owner-Key": OWNER_KEY}))


def test_owner_key_must_be_strong_and_is_never_printed() -> None:
    with pytest.raises(ValidationError, match="at least 16"):
        Settings(_env_file=None, owner_key="short")
    assert Settings(_env_file=None, owner_key="  ").owner_key is None
    s = Settings(_env_file=None, owner_key=OWNER_KEY)
    assert OWNER_KEY not in repr(s) and OWNER_KEY not in str(s)
