"""The Alembic migrations must produce exactly the schema the models declare."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine

from aeids.db.models import UNMAPPED, Base


def _mapped(_obj, name, _type, _reflected, _compare_to) -> bool:
    return name not in UNMAPPED


def test_migrations_match_models(engine: Engine) -> None:
    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "include_object": _mapped})
        assert compare_metadata(ctx, Base.metadata) == []
