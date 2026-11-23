"""Security review (D-034): the local API refuses other host names and cross-site writes, and
printed settings never show the database password."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aeids.api.security import host_name
from aeids.config import Settings
from tests.factories import pdf_bytes


@pytest.mark.parametrize("host", ["attacker.example", "attacker.example:8001", "127.0.0.1.nip.io", "192.168.1.5:8001"])
def test_a_foreign_host_name_is_refused(client: TestClient, host: str) -> None:
    """DNS rebinding: a site that points its own name at 127.0.0.1 gets nothing back."""
    for path in ("/health", "/api/v1/workflows", "/ui/", "/api/v1/dashboard/overview"):
        resp = client.get(path, headers={"host": host})
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "host_not_allowed", (path, host)


@pytest.mark.parametrize("host", ["127.0.0.1:8001", "localhost:8001", "[::1]:8001", "LOCALHOST", "127.0.0.1"])
def test_this_machine_is_answered(client: TestClient, host: str) -> None:
    assert client.get("/health", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("headers", [{"origin": "https://attacker.example"}, {"origin": "null"},
                                     {"sec-fetch-site": "cross-site"},
                                     {"origin": "http://127.0.0.1:8001", "sec-fetch-site": "cross-site"}])
def test_a_cross_site_upload_is_refused_and_nothing_is_stored(client: TestClient, headers: dict) -> None:
    resp = client.post("/api/v1/documents", data={"company_name": "Apple Inc."}, headers=headers,
                       files={"file": ("x.pdf", pdf_bytes(1, "csrf"), "application/pdf")})
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "cross_site_request"
    assert client.get("/api/v1/documents").json() == []


@pytest.mark.parametrize("path, body", [("/api/v1/workflows", {"query": "What were Apple's total net sales?"}),
                                        ("/api/v1/dashboard/workflows", {"query": "What were Apple's total net sales?"})])
def test_cross_site_questions_are_refused(client: TestClient, path: str, body: dict) -> None:
    resp = client.post(path, json=body, headers={"origin": "https://attacker.example"})
    assert resp.status_code == 403


def test_the_console_and_scripts_can_still_write(client: TestClient) -> None:
    same_site = {"origin": "http://testserver", "sec-fetch-site": "same-origin"}
    resp = client.post("/api/v1/documents", data={"company_name": "Apple Inc."}, headers=same_site,
                       files={"file": ("a.pdf", pdf_bytes(1, "ok"), "application/pdf")})
    assert resp.status_code == 201, resp.text
    resp = client.post("/api/v1/documents", data={"company_name": "Apple Inc."},  # curl / CLI: no Origin
                       files={"file": ("b.pdf", pdf_bytes(1, "ok2"), "application/pdf")})
    assert resp.status_code == 201, resp.text


def test_reads_from_another_site_are_left_to_the_browser(client: TestClient) -> None:
    """No CORS headers, so a browser never hands the answer to the other site's page."""
    resp = client.get("/api/v1/workflows", headers={"origin": "https://attacker.example"})
    assert resp.status_code == 200 and "access-control-allow-origin" not in resp.headers


def test_host_name_parsing() -> None:
    assert [host_name(h) for h in ("LocalHost:8001", "[::1]:8001", "::1", "127.0.0.1", "")] == \
        ["localhost", "::1", "::1", "127.0.0.1", ""]


def test_printed_settings_never_show_the_database_password() -> None:
    s = Settings(_env_file=None, database_url="postgresql+psycopg://aeids:S3cret-pw@127.0.0.1:5432/aeids")
    assert "S3cret-pw" not in repr(s) and "S3cret-pw" not in str(s)
    assert s.database_url.endswith("/aeids")  # the value itself is unchanged
