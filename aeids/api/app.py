"""FastAPI app factory.

    uvicorn aeids.api.app:create_app --factory --port 8001
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from aeids import tracing
from aeids.api.routers import dashboard, documents, ingestion, search, workflows
from aeids.config import Settings, get_settings
from aeids.db.session import make_engine, make_sessionmaker
from aeids.documents import DocumentError
from aeids.storage import LocalStore


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        for name in ("vector_index", "tablelens"):  # clients made on first use
            client = getattr(_app.state, name, None)
            if client is not None:
                client.close()
        embedder = getattr(_app.state, "embedder", None)
        if embedder is not None and hasattr(embedder, "client"):
            embedder.client.close()
        engine.dispose()

    app = FastAPI(
        title="AEIDS",
        version="0.1.0",
        description="Autonomous Enterprise Intelligence & Decision System — decision support, "
        "not final legal, financial or compliance determinations.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.store = LocalStore(settings.storage_root)
    if settings.tracing_enabled:
        tracing.configure(tracing.SqlTraceSink(engine))

    @app.exception_handler(DocumentError)
    async def _document_error(_request: Request, exc: DocumentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health", tags=["health"])
    def health() -> JSONResponse:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 — report unavailability, never the error details
            return JSONResponse(status_code=503, content={"status": "unavailable", "db": "down"})
        return JSONResponse(content={"status": "ok", "db": "ok"})

    app.include_router(documents.router)
    app.include_router(ingestion.router)
    app.include_router(search.router)
    app.include_router(workflows.router)
    app.include_router(dashboard.router)
    dashboard.mount_ui(app)
    return app
