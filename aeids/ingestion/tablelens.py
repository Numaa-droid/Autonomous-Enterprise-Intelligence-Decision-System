"""Client for the TableLens server (``tablelens/app.py``), which parses a PDF with the
user's models: fine-tuned Table Transformer, docling layout, PaddleOCR-VL. TableLens stays its
own project (D-015); AEIDS only calls its HTTP API (D-019):

    POST /detect      file=<pdf>                      -> {"job": id}   tables + layout
    POST /recognise   name=<stem>, mode=all           -> {"job": id}   cells, text, charts, order
    GET  /events/{id}?since=n -> {"events": [...], "next": n, "done": bool}
    GET  /output/<stem>/structure/<stem>_structure.json                the result

When the TableLens Modal GPU server is deployed, ``app.py`` forwards the jobs to it itself.

TableLens stops a job nobody polls for 60 s, so ``wait`` polls every ``poll_seconds`` (<= 10).
It runs one job at a time; a job sent while another runs waits there, still polled.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

MAX_POLL_SECONDS = 10.0  # TableLens drops a job not polled for 60 s


class TableLensError(Exception):
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TableLensUnavailable(TableLensError):
    """Server down, unreachable, 5xx, or the job vanished (server restarted): try again later."""

    retryable = True


class TableLensJobFailed(TableLensError):
    """TableLens ran the job and reported an error, or refused the request (4xx)."""


class TableLensTimeout(TableLensError):
    retryable = True


def tablelens_stem(filename: str, checksum: str) -> str:
    """The name AEIDS gives a PDF on the TableLens side: its file stem, TableLens' own sanitising
    (``app.safe_stem``), plus 12 checksum characters so two different files never share a
    TableLens result (a replacement version usually keeps the file name)."""
    stem = re.sub(r"[^\w.-]+", "_", Path(filename).stem).strip("_") or "document"
    return f"{stem[:80]}-{checksum[:12]}"


@dataclass(frozen=True)
class JobResult:
    job_id: str
    final_event: dict[str, Any]
    warnings: list[str]
    seconds: float


class TableLensClient:
    def __init__(
        self,
        base_url: str,
        *,
        poll_seconds: float = 3.0,
        job_timeout_seconds: float = 4 * 3600,
        request_timeout_seconds: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.poll_seconds = min(poll_seconds, MAX_POLL_SECONDS)
        self.job_timeout_seconds = job_timeout_seconds
        self._sleep = sleep
        self._clock = clock
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=base_url.rstrip("/"), transport=transport,
                                  timeout=httpx.Timeout(request_timeout_seconds, connect=10.0))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TableLensClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---------- public ----------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health").json()

    def existing_structure(self, stem: str) -> bytes | None:
        """A result TableLens already has for ``stem``, or None."""
        try:
            resp = self._http.get(f"/output/{stem}/structure/{stem}_structure.json")
        except httpx.TransportError as exc:
            raise TableLensUnavailable(f"TableLens not reachable: {type(exc).__name__}") from None
        if resp.status_code == 404:
            return None
        self._check(resp, "download the structure")
        return resp.content

    def parse(
        self,
        pdf: Path,
        stem: str,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[bytes, list[str]]:
        """Run detection then recognition (mode "all") on ``pdf`` as ``stem``; return the
        ``_structure.json`` bytes and the warnings TableLens logged."""
        with pdf.open("rb") as f:
            resp = self._request("POST", "/detect",
                                 files={"file": (f"{stem}.pdf", f, "application/pdf")})
        detect = self.wait(self._job_id(resp), "summary", on_event)
        resp = self._request("POST", "/recognise", data={"name": stem, "mode": "all"})
        recognise = self.wait(self._job_id(resp), "rsummary", on_event)
        data = self.existing_structure(stem)
        if data is None:
            raise TableLensJobFailed(f"recognition finished but no _structure.json for {stem}")
        return data, detect.warnings + recognise.warnings

    def wait(
        self,
        job_id: str,
        final_type: str,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> JobResult:
        started = self._clock()
        since, final, warnings = 0, None, []
        while True:
            resp = self._request("GET", f"/events/{job_id}", params={"since": since},
                                 missing="job expired on the TableLens side (server restarted?)")
            body = resp.json()
            for ev in body.get("events", []):
                kind = ev.get("type")
                if kind == "error":
                    raise TableLensJobFailed(f"TableLens job failed: {ev.get('detail')}")
                if kind == "log" and ev.get("level") in ("warn", "err"):
                    warnings.append(str(ev.get("msg"))[:300])
                if kind == final_type:
                    final = ev
                if on_event is not None and kind != "page":  # page events carry big images
                    on_event(ev)
            since = body.get("next", since)
            if body.get("done"):
                break
            if self._clock() - started > self.job_timeout_seconds:
                raise TableLensTimeout(f"TableLens job {job_id} still running after "
                                       f"{self.job_timeout_seconds:.0f} s")
            self._sleep(self.poll_seconds)
        if final is None:
            raise TableLensJobFailed(f"TableLens job {job_id} ended without a {final_type} event")
        return JobResult(job_id, final, warnings, self._clock() - started)

    # ---------- transport ----------

    def _job_id(self, resp: httpx.Response) -> str:
        job = resp.json().get("job")
        if not isinstance(job, str) or not job:
            raise TableLensJobFailed("TableLens did not return a job id")
        return job

    def _request(self, method: str, path: str, *, missing: str | None = None,
                 **kwargs: Any) -> httpx.Response:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise TableLensUnavailable(f"TableLens not reachable: {type(exc).__name__}") from None
        if resp.status_code == 404 and missing:
            raise TableLensUnavailable(missing)
        self._check(resp, f"{method} {path}")
        return resp

    def _check(self, resp: httpx.Response, what: str) -> None:
        if resp.status_code < 400:
            return
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text[:200]
        message = f"TableLens {what}: HTTP {resp.status_code}: {detail}"
        if resp.status_code >= 500:
            raise TableLensUnavailable(message)
        raise TableLensJobFailed(message)
