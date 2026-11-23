"""Test fixtures. SQLite by default; set AEIDS_TEST_DATABASE_URL to run against Postgres.

The schema always comes from the Alembic migrations, so tests also exercise them.
The Postgres test database is wiped (downgrade to base) at the start and end of the run.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from aeids import tracing
from aeids.api.app import create_app
from aeids.config import Settings
from aeids.db.models import Base
from aeids.db.session import make_engine, make_sessionmaker
from aeids.llm.embedding import HashEmbedder
from aeids.retrieval.vector_index import VectorIndex
from aeids.storage import LocalStore

ROOT = Path(__file__).resolve().parents[1]
MAX_UPLOAD_BYTES = 64 * 1024


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    url = os.environ.get("AEIDS_TEST_DATABASE_URL") or (
        f"sqlite:///{tmp_path_factory.mktemp('db') / 'test.db'}"
    )
    cfg = alembic_config(url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield url
    command.downgrade(cfg, "base")


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    with eng.begin() as conn:  # empty every table between tests
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with make_sessionmaker(engine)() as s:
        yield s


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    return tmp_path / "storage"


@pytest.fixture
def store(storage_root: Path) -> LocalStore:
    return LocalStore(storage_root)


@pytest.fixture
def settings(database_url: str, storage_root: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        storage_root=storage_root,
        max_upload_bytes=MAX_UPLOAD_BYTES,
        jobs_mode="off",  # tests that need processing switch it on with a fake TableLens
        allowed_hosts=["127.0.0.1", "localhost", "::1", "testserver"],  # TestClient sends Host: testserver
    )


@pytest.fixture(autouse=True)
def _tracing_off_after_test() -> Iterator[None]:
    """create_app points tracing at the test database; nothing leaks into the next test."""
    yield
    tracing.configure(None)


@pytest.fixture
def vector_index() -> Iterator[VectorIndex]:
    """An in-memory Qdrant (no server needed)."""
    index = VectorIndex(QdrantClient(":memory:"))
    yield index
    index.close()


@pytest.fixture
def embedder() -> HashEmbedder:
    return HashEmbedder(64)


@pytest.fixture
def client(settings: Settings, engine: Engine, vector_index: VectorIndex,
           embedder: HashEmbedder) -> Iterator[TestClient]:
    # ``engine`` is requested so tables are emptied after the test. Indexing and search use an
    # in-memory Qdrant and the offline HashEmbedder (no network).
    app = create_app(settings)
    app.state.vector_index = vector_index
    app.state.embedder = embedder
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_pdf() -> Callable[[str], bytes]:
    """Minimal bytes that pass the PDF magic check; ``tag`` makes the checksum unique."""

    def _make(tag: str) -> bytes:
        return b"%PDF-1.4\n% " + tag.encode() + b"\n%%EOF\n"

    return _make
