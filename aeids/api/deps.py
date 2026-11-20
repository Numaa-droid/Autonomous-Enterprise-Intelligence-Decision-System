"""Request dependencies: DB session, object store, settings and the caller's tenant."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from aeids.config import Settings
from aeids.documents import get_or_create_tenant
from aeids.storage import ObjectStore


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session


def get_store(request: Request) -> ObjectStore:
    return request.app.state.store


def get_tenant_id(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> uuid.UUID:
    """Release 0 uses one default tenant (MVP_PLAN.md §1); auth replaces this later."""
    cached: uuid.UUID | None = getattr(request.app.state, "default_tenant_id", None)
    if cached is not None:
        return cached
    tenant = get_or_create_tenant(session, request.app.state.settings.default_tenant_name)
    session.commit()
    request.app.state.default_tenant_id = tenant.id
    return tenant.id


SessionDep = Annotated[Session, Depends(get_session)]
StoreDep = Annotated[ObjectStore, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
TenantDep = Annotated[uuid.UUID, Depends(get_tenant_id)]
