"""Request guards for an API that runs on the user's own machine (security review, D-034).

The API listens on 127.0.0.1, so other machines cannot reach it, but a web page in the user's
browser can. Two guards close that path:

* **Host check (DNS rebinding).** A site can point its own name at 127.0.0.1; the browser then
  treats this API as that site and lets the page read every answer. Requests whose ``Host`` is not
  one of ``AEIDS_ALLOWED_HOSTS`` (default ``127.0.0.1``, ``localhost``, ``::1``) are refused (400).
* **Cross-site writes (CSRF).** A page on another site can send a form POST (for example an upload)
  without the browser asking first. A POST, PUT, PATCH or DELETE that a browser marks as coming
  from another origin (``Origin`` not an allowed host, or ``Sec-Fetch-Site: cross-site``) is
  refused (403). Requests without those headers (curl, scripts, the CLI) are not affected.

Reads from another origin need no guard here: without CORS headers the browser never hands the
response to the page.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def host_name(value: str) -> str:
    """The host part of a ``Host`` header or URL netloc, lower case, without port or brackets."""
    value = value.strip().lower()
    if value.startswith("["):  # [::1]:8001
        return value[1:].split("]", 1)[0]
    if value.count(":") > 1:  # a bare IPv6 address
        return value
    return value.split(":", 1)[0]


class LocalOnlyGuard:
    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]], allowed_hosts: Iterable[str]) -> None:
        self.app = app
        self.allowed = {host_name(h) for h in allowed_hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if host_name(headers.get("host", "")) not in self.allowed:
            await _refuse(scope, send, 400, "host_not_allowed",
                          "This API only answers requests addressed to this machine (see AEIDS_ALLOWED_HOSTS).")
            return
        if scope["type"] == "http" and scope.get("method", "GET").upper() in UNSAFE_METHODS:
            origin = headers.get("origin")
            cross = headers.get("sec-fetch-site", "").lower() == "cross-site"
            foreign = origin is not None and (origin == "null" or host_name(urlsplit(origin).netloc) not in self.allowed)
            if cross or foreign:
                await _refuse(scope, send, 403, "cross_site_request", "Changes cannot be requested from another website.")
                return
        await self.app(scope, receive, send)


# The only changes a read-only demo accepts: searching and asking a question (D-039).
READ_ONLY_ALLOWED = {
    ("POST", "/api/v1/search"),
    ("POST", "/api/v1/workflows"),
    ("POST", "/api/v1/dashboard/search"),
    ("POST", "/api/v1/dashboard/workflows"),
}
READ_ONLY_MESSAGE = ("This is a read-only demo: uploading, deleting and reviewing are turned off. "
                     "You can still browse the reports, search them and ask questions.")


class ReadOnlyGuard:
    """``AEIDS_READ_ONLY=true`` (public demo, D-039): every POST, PUT, PATCH or DELETE is refused
    (403) unless it is in ``READ_ONLY_ALLOWED``. An allow-list, so an endpoint added later is
    read-only until it is listed here."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            method = scope.get("method", "GET").upper()
            path = scope.get("path", "").rstrip("/") or "/"
            if method in UNSAFE_METHODS and (method, path) not in READ_ONLY_ALLOWED:
                await _refuse(scope, send, 403, "read_only", READ_ONLY_MESSAGE)
                return
        await self.app(scope, receive, send)


async def _refuse(scope: Scope, send: Send, status: int, code: str, message: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
