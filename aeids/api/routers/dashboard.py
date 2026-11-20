"""Data for the web dashboard (``/ui``): overview, documents, search, traces, evaluations, logs.

A local operator view (D-022): it reads any tenant by name (``?tenant=eval``) and every trace,
so like the rest of the API it has no login yet and must stay on 127.0.0.1 (full auth is out
of Release 0 scope). Document text and trace inputs/outputs are untrusted data (rule 7): the
page renders them as text only. Logs are read from fixed file names under ``log_dir``.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from aeids import tracing
from aeids.api.deps import SessionDep, SettingsDep
from aeids.api.processing import embedder, vector_index
from aeids.db.models import Company, Document, DocumentChunk, DocumentVersion, Tenant, TraceSpan
from aeids.documents import DocumentError
from aeids.retrieval.search import SearchFilters, search

ROOT = Path(__file__).resolve().parents[3]
WEB = Path(__file__).resolve().parents[2] / "web"
REPORTS = ROOT / "reports"
LOG_SOURCES = ("tablelens", "api", "worker", "start")
_REPORT_NAME = re.compile(r"retrieval-\d{8}T\d{6}Z\.json")
_LEVEL = re.compile(r"\b(CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG)\b|Traceback|error:", re.I)
_LEADING_LEVEL = re.compile(r"^(CRITICAL|ERROR|WARNING|INFO|DEBUG):?\s+")

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
TenantName = Annotated[str | None, Query(max_length=200)]


class NotFound(DocumentError):
    code = "not_found"
    http_status = 404


class _QuietDashboard(logging.Filter):
    """Drop uvicorn access lines for the dashboard's own page loads and polling, so the API log
    (and the Logs page in Live mode) is not flooded by itself."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        return not (len(args) >= 3 and args[1] == "GET"
                    and str(args[2]).startswith(("/api/v1/dashboard/", "/ui/")))


def mount_ui(app: FastAPI) -> None:
    """The dashboard page at /ui (static files, no build step); / redirects there."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _QuietDashboard) for f in access.filters):
        access.addFilter(_QuietDashboard())
    if WEB.is_dir():
        app.mount("/ui", StaticFiles(directory=WEB, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def _root() -> RedirectResponse:
            return RedirectResponse("/ui/")


def _tenant(session: Session, settings: Any, name: str | None, *, required: bool = True
            ) -> Tenant | None:
    tenant = session.scalar(select(Tenant).where(
        Tenant.name == (name or settings.default_tenant_name)))
    # the default tenant only exists after the first upload: optional views show it as empty
    if tenant is None and (required or (name and name != settings.default_tenant_name)):
        raise NotFound(f"no tenant named {name or settings.default_tenant_name!r}")
    return tenant


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite returns naive UTC


def _maybe(dt: datetime | None) -> datetime | None:
    return _aware(dt) if dt else None


def _pct(values: list[float], p: int) -> float:
    if len(values) == 1:
        return round(values[0], 1)
    return round(statistics.quantiles(values, n=100, method="inclusive")[p - 1], 1)


# --- overview -------------------------------------------------------------------

@router.get("/tenants")
def tenants(session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    names = list(session.scalars(select(Tenant.name).order_by(Tenant.name)))
    return {"default": settings.default_tenant_name, "tenants": names,
            "models": {"embedding": settings.embedding_model, "llm": settings.llm_model}}


@router.get("/overview")
def overview(session: SessionDep, settings: SettingsDep, tenant: TenantName = None) -> dict[str, Any]:
    t = _tenant(session, settings, tenant, required=False)
    since = datetime.now(UTC) - timedelta(days=14)
    spans = session.execute(
        select(TraceSpan.parent_span_id, TraceSpan.kind, TraceSpan.status, TraceSpan.started_at,
               TraceSpan.duration_ms, TraceSpan.total_tokens, TraceSpan.cost_usd, TraceSpan.name)
        .where(TraceSpan.started_at >= since).order_by(TraceSpan.started_at).limit(100_000)
    ).all()
    today = datetime.now(UTC).date()
    days = {(today - timedelta(days=i)).isoformat(): {"traces": 0, "spans": 0, "errors": 0,
                                                       "tokens": 0, "cost": Decimal(0)}
            for i in range(13, -1, -1)}
    latency: dict[str, list[float]] = defaultdict(list)
    week = datetime.now(UTC) - timedelta(days=7)
    totals: Counter[str] = Counter()
    cost_7d = Decimal(0)
    for s in spans:
        started = _aware(s.started_at)
        root = s.parent_span_id is None
        d = days.get(started.date().isoformat())
        if d is not None:
            d["spans"] += 1
            d["traces"] += root
            d["errors"] += root and s.status == "error"
            d["tokens"] += s.total_tokens or 0
            d["cost"] += s.cost_usd or 0
        if s.duration_ms is not None and s.kind not in ("eval", "chain"):  # containers, not steps
            latency[s.kind].append(s.duration_ms)
        if started >= week:
            totals["spans"] += 1
            totals["traces"] += root
            totals["errors"] += root and s.status == "error"
            totals["tokens"] += s.total_tokens or 0
            totals["llm_calls"] += s.kind == "llm"
            totals["embedding_calls"] += s.kind == "embedding"
            totals["searches"] += s.name == "search"
            cost_7d += s.cost_usd or 0
    return {
        "tenant": t.name if t else (tenant or settings.default_tenant_name),
        "documents": _document_stats(session, t.id) if t else _document_stats(session, None),
        "totals_7d": {k: totals.get(k, 0) for k in ("spans", "traces", "errors", "tokens",
                                                    "llm_calls", "embedding_calls", "searches")}
                     | {"cost_usd": str(cost_7d)},
        "activity": [{"date": k, **v, "cost": str(v["cost"])} for k, v in days.items()],
        "latency": sorted(({"kind": k, "count": len(v), "p50": _pct(v, 50), "p95": _pct(v, 95)}
                           for k, v in latency.items()), key=lambda x: -x["p95"]),
        "recent_traces": _trace_rows(session, limit=6),
        "models": {"embedding": settings.embedding_model, "llm": settings.llm_model},
    }


def _document_stats(session: Session, tenant_id: uuid.UUID | None) -> dict[str, Any]:
    if tenant_id is None:
        return {"documents": 0, "companies": 0, "versions": 0, "by_status": {}, "chunks": 0,
                "chunk_kinds": {}, "indexed_points": 0, "failures": []}
    by_status = dict(session.execute(
        select(DocumentVersion.parser_status, func.count())
        .where(DocumentVersion.tenant_id == tenant_id).group_by(DocumentVersion.parser_status)
    ).all())
    kind = DocumentChunk.meta["kind"].as_string()
    kinds = dict(session.execute(
        select(kind, func.count())
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.version_id)
        .where(DocumentChunk.tenant_id == tenant_id, DocumentVersion.is_current.is_(True))
        .group_by(kind)).all())
    failures = session.execute(
        select(DocumentVersion.filename, DocumentVersion.parser_error, DocumentVersion.uploaded_at)
        .where(DocumentVersion.tenant_id == tenant_id, DocumentVersion.parser_status == "failed")
        .order_by(DocumentVersion.uploaded_at.desc()).limit(5)).all()
    return {
        "documents": session.scalar(select(func.count()).select_from(Document)
                                    .where(Document.tenant_id == tenant_id)) or 0,
        "companies": session.scalar(select(func.count()).select_from(Company)
                                    .where(Company.tenant_id == tenant_id)) or 0,
        "versions": sum(by_status.values()),
        "by_status": by_status,
        "chunks": sum(kinds.values()),
        "chunk_kinds": {k or "text": v for k, v in kinds.items()},
        "indexed_points": int(session.scalar(
            select(func.coalesce(func.sum(DocumentVersion.index_point_count), 0))
            .where(DocumentVersion.tenant_id == tenant_id,
                   DocumentVersion.is_current.is_(True))) or 0),
        "failures": [{"file": f, "error": e, "at": _aware(a)} for f, e, a in failures],
    }


@router.get("/services")
def services(request: Request, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    """Reachability of the services AEIDS uses (short timeouts; no secrets in the answer)."""
    out: dict[str, Any] = {"database": {"ok": True, "detail": session.get_bind().dialect.name}}
    try:
        names = vector_index(request.app.state).client.get_collections().collections
        out["qdrant"] = {"ok": True, "detail": f"{len(names)} collection(s)"}
    except Exception as exc:  # noqa: BLE001 — report reachability only
        out["qdrant"] = {"ok": False, "detail": type(exc).__name__}
    try:
        from redis import Redis

        Redis.from_url(settings.redis_url, socket_timeout=1, socket_connect_timeout=1).ping()
        out["redis"] = {"ok": True, "detail": "job queue reachable"}
    except Exception as exc:  # noqa: BLE001
        out["redis"] = {"ok": False, "detail": type(exc).__name__}
    try:
        h = httpx.get(settings.tablelens_url.rstrip("/") + "/health", timeout=1.5).json()
        out["tablelens"] = {"ok": True, "detail": "GPU server" if h.get("gpu_server") else "local"}
    except Exception as exc:  # noqa: BLE001
        out["tablelens"] = {"ok": False, "detail": type(exc).__name__}
    key = settings.openrouter_api_key
    has_key = bool(key and key.get_secret_value().strip())
    out["openrouter"] = {"ok": has_key, "detail": "API key set" if has_key else "no API key"}
    return out


# --- documents ------------------------------------------------------------------

@router.get("/documents")
def documents(session: SessionDep, settings: SettingsDep,
              tenant: TenantName = None) -> list[dict[str, Any]]:
    t = _tenant(session, settings, tenant)
    chunk_counts = dict(session.execute(
        select(DocumentChunk.version_id, func.count()).where(DocumentChunk.tenant_id == t.id)
        .group_by(DocumentChunk.version_id)).all())
    rows = session.execute(
        select(Document, Company.name).join(Company, Company.id == Document.company_id)
        .where(Document.tenant_id == t.id).order_by(Company.name, Document.created_at)).all()
    return [{
        "id": doc.id, "company": company, "filename": doc.filename,
        "document_type": doc.document_type, "fiscal_year": doc.fiscal_year,
        "versions": [{
            "id": v.id, "version_number": v.version_number, "filename": v.filename,
            "size_bytes": v.size_bytes, "uploaded_at": _aware(v.uploaded_at), "is_current": v.is_current,
            "status": v.parser_status, "error": v.parser_error, "parsed_at": _maybe(v.parsed_at),
            "indexed_at": _maybe(v.indexed_at), "chunks": chunk_counts.get(v.id, 0),
            "points": v.index_point_count, "warnings": v.parse_warnings or [],
        } for v in doc.versions],
    } for doc, company in rows]


# --- search playground ------------------------------------------------------------

class PlaygroundSearch(BaseModel):
    tenant: str | None = Field(default=None, max_length=200)
    query: str = Field(min_length=1, max_length=2000)
    mode: Literal["hybrid", "dense", "keyword"] = "hybrid"
    top_k: int = Field(default=10, ge=1, le=50)
    include_superseded: bool = False


@router.post("/search")
def playground_search(body: PlaygroundSearch, request: Request, session: SessionDep,
                      settings: SettingsDep) -> dict[str, Any]:
    t = _tenant(session, settings, body.tenant)
    state = request.app.state
    with tracing.span("dashboard.search", "chain", tenant_id=t.id,
                      inputs={"query": body.query, "tenant": t.name}) as sp:
        result = search(session, vector_index(state), embedder(state), t.id, body.query,
                        mode=body.mode, top_k=body.top_k,
                        filters=SearchFilters(include_superseded=body.include_superseded))
        trace_id = sp.trace_id
    return {"trace_id": trace_id, "query": result.query, "mode": result.mode,
            "warnings": result.warnings, "hits": [h.__dict__ for h in result.hits]}


# --- workflows (console: any tenant by name) -----------------------------------------

class PlaygroundWorkflow(BaseModel):
    tenant: str | None = Field(default=None, max_length=200)
    query: str = Field(min_length=3, max_length=2000)


@router.post("/workflows", status_code=202)
def console_workflow(body: PlaygroundWorkflow, request: Request, session: SessionDep,
                     settings: SettingsDep) -> dict[str, Any]:
    from aeids.api.routers.workflows import WorkflowIn, start

    t = _tenant(session, settings, body.tenant)
    return start(request, session, t.id, WorkflowIn(query=body.query))


@router.get("/workflows")
def console_workflows(session: SessionDep, settings: SettingsDep, tenant: TenantName = None) -> list[dict[str, Any]]:
    from aeids.orchestration import views

    t = _tenant(session, settings, tenant, required=False)
    return views.list_workflows(session, t.id) if t else []


@router.get("/workflows/{workflow_id}")
def console_workflow_detail(workflow_id: uuid.UUID, session: SessionDep, settings: SettingsDep,
                            tenant: TenantName = None) -> dict[str, Any]:
    from aeids.orchestration import views

    t = _tenant(session, settings, tenant)
    return views.detail(session, views.get_workflow(session, t.id, workflow_id))


# --- traces ---------------------------------------------------------------------

def _trace_rows(session: Session, *, limit: int, offset: int = 0, kind: str | None = None,
                status: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
    stmt = select(TraceSpan).where(TraceSpan.parent_span_id.is_(None))
    if kind:
        stmt = stmt.where(TraceSpan.kind == kind)
    if status:
        stmt = stmt.where(TraceSpan.status == status)
    if q:
        stmt = stmt.where(TraceSpan.name.ilike(f"%{q}%"))
    roots = list(session.scalars(stmt.order_by(TraceSpan.started_at.desc())
                                 .limit(limit).offset(offset)))
    if not roots:
        return []
    agg = {row.trace_id: row for row in session.execute(
        select(TraceSpan.trace_id, func.count().label("spans"),
               func.coalesce(func.sum(TraceSpan.total_tokens), 0).label("tokens"),
               func.coalesce(func.sum(TraceSpan.cost_usd), 0).label("cost"),
               func.sum(cast(TraceSpan.status == "error", Integer)).label("errors"),
               func.sum(cast(TraceSpan.kind.in_(("llm", "embedding")), Integer)).label("calls"))
        .where(TraceSpan.trace_id.in_([r.trace_id for r in roots]))
        .group_by(TraceSpan.trace_id))}
    tenant_names = dict(session.execute(select(Tenant.id, Tenant.name)).all())
    out = []
    for r in roots:
        a = agg.get(r.trace_id)
        out.append({
            "trace_id": r.trace_id, "name": r.name, "kind": r.kind, "status": r.status,
            "error": r.error, "started_at": _aware(r.started_at), "duration_ms": r.duration_ms,
            "tenant": tenant_names.get(r.tenant_id), "preview": _preview(r.inputs),
            "spans": a.spans if a else 1, "tokens": int(a.tokens) if a else 0,
            "cost_usd": str(a.cost) if a else "0", "errors": int(a.errors or 0) if a else 0,
            "model_calls": int(a.calls or 0) if a else 0,
        })
    return out


def _preview(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("query", "file", "filename", "questions", "version_id"):
            if key in value:
                return f"{key}: {value[key]}"[:160]
    return (json.dumps(value, default=str) if value is not None else "")[:160]


@router.get("/traces")
def traces(session: SessionDep, limit: Annotated[int, Query(ge=1, le=200)] = 50,
           offset: Annotated[int, Query(ge=0)] = 0,
           kind: Annotated[str | None, Query(max_length=30)] = None,
           status: Annotated[Literal["ok", "error"] | None, Query()] = None,
           q: Annotated[str | None, Query(max_length=100)] = None) -> dict[str, Any]:
    total = session.scalar(select(func.count()).select_from(TraceSpan)
                           .where(TraceSpan.parent_span_id.is_(None))) or 0
    return {"total": total, "traces": _trace_rows(session, limit=limit, offset=offset, kind=kind,
                                                  status=status, q=q)}


@router.get("/traces/{trace_id}")
def trace(trace_id: str, session: SessionDep) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", trace_id):
        raise NotFound("no such trace")
    spans = list(session.scalars(select(TraceSpan).where(TraceSpan.trace_id == trace_id)
                                 .order_by(TraceSpan.started_at)))
    if not spans:
        raise NotFound("no such trace")
    start = min(_aware(s.started_at) for s in spans)
    tenant_names = dict(session.execute(select(Tenant.id, Tenant.name)).all())
    return {"trace_id": trace_id, "started_at": start, "spans": [{
        "span_id": s.span_id, "parent_span_id": s.parent_span_id, "name": s.name, "kind": s.kind,
        "status": s.status, "error": s.error, "started_at": _aware(s.started_at),
        "offset_ms": round((_aware(s.started_at) - start).total_seconds() * 1000, 2),
        "duration_ms": s.duration_ms, "inputs": s.inputs, "outputs": s.outputs,
        "attributes": s.attributes, "model": s.model, "total_tokens": s.total_tokens,
        "cost_usd": str(s.cost_usd) if s.cost_usd is not None else None,
        "tenant": tenant_names.get(s.tenant_id),
    } for s in spans]}


# --- evaluations ----------------------------------------------------------------

@router.get("/evals")
def evals() -> list[dict[str, Any]]:
    out = []
    for path in sorted(REPORTS.glob("retrieval-*.json"), reverse=True):
        if not _REPORT_NAME.fullmatch(path.name):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        out.append({"name": path.name, "created_at": data.get("created_at"),
                    "embedding_model": data.get("embedding_model"), "scores": data.get("scores"),
                    "questions": len(data.get("questions", [])), "corpus": data.get("corpus", [])})
    return out


@router.get("/evals/{name}")
def eval_run(name: str) -> dict[str, Any]:
    if not _REPORT_NAME.fullmatch(name) or not (REPORTS / name).is_file():
        raise NotFound("no such evaluation run")
    return json.loads((REPORTS / name).read_text())


# --- logs -----------------------------------------------------------------------

@router.get("/logs")
def logs(settings: SettingsDep,
         source: Annotated[Literal["all", "tablelens", "api", "worker", "start"], Query()] = "all",
         lines: Annotated[int, Query(ge=10, le=2000)] = 300,
         q: Annotated[str | None, Query(max_length=100)] = None) -> dict[str, Any]:
    directory = settings.log_dir if settings.log_dir.is_absolute() else ROOT / settings.log_dir
    entries: list[dict[str, Any]] = []
    available = []
    for name in (LOG_SOURCES if source == "all" else (source,)):
        path = directory / f"{name}.log"
        if not path.is_file():
            continue
        available.append(name)
        for raw in _tail(path, lines):
            ts, _, text = raw.partition(" ")
            if not re.match(r"\d{4}-\d\d-\d\dT", ts):
                ts, text = "", raw
            if q and q.casefold() not in text.casefold():
                continue
            m = _LEVEL.search(text)
            level = (m.group(1) or "ERROR").upper() if m else "INFO"
            entries.append({"ts": ts, "source": name, "text": _LEADING_LEVEL.sub("", text),
                            "level": "WARNING" if level == "WARN" else level})
    entries.sort(key=lambda e: e["ts"])
    return {"sources": available, "entries": entries[-lines:],
            "hint": None if available else "No log files yet: start everything with ./start.sh"}


def _tail(path: Path, lines: int, block: int = 65536) -> list[str]:
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        data = b""
        while size > 0 and data.count(b"\n") <= lines:
            step = min(block, size)
            size -= step
            fh.seek(size)
            data = fh.read(step) + data
    return data.decode("utf-8", "replace").splitlines()[-lines:]
