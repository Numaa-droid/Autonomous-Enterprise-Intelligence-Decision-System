"""Workflows: ask a question, watch the task graph, read verified findings (M3)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from aeids.api.deps import SessionDep, TenantDep
from aeids.api.processing import registry, schedule_workflow
from aeids.api.schemas import ErrorOut
from aeids.orchestration import supervisor as sup
from aeids.orchestration import views

router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])


class WorkflowIn(BaseModel):
    query: str = Field(min_length=3, max_length=2000)
    company_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    fiscal_years: list[int] = Field(default_factory=list, max_length=20)

    def scope(self) -> dict[str, Any]:
        return {"company_ids": [str(x) for x in self.company_ids],
                "document_ids": [str(x) for x in self.document_ids], "fiscal_years": self.fiscal_years}


def start(request: Request, session, tenant_id: uuid.UUID, body: WorkflowIn) -> dict[str, Any]:
    state = request.app.state
    wf = sup.create_workflow(session, tenant_id, body.query, body.scope(), settings=state.settings,
                             registry=registry(state))
    processing = schedule_workflow(state, wf.id)
    session.expire_all()
    return {**views.summary(session, views.get_workflow(session, tenant_id, wf.id)), "processing": processing}


@router.post("", status_code=status.HTTP_202_ACCEPTED, responses={422: {"model": ErrorOut}})
def create(body: WorkflowIn, request: Request, session: SessionDep, tenant_id: TenantDep) -> dict[str, Any]:
    """Plan a workflow for a question and start it. The Supervisor makes the task graph; the
    worker runs it (``processing``: queued / done / off / not_queued)."""
    return start(request, session, tenant_id, body)


@router.get("")
def list_all(session: SessionDep, tenant_id: TenantDep) -> list[dict[str, Any]]:
    return views.list_workflows(session, tenant_id)


@router.get("/{workflow_id}", responses={404: {"model": ErrorOut}})
def get(workflow_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> dict[str, Any]:
    wf = views.get_workflow(session, tenant_id, workflow_id)
    return {**views.summary(session, wf), "plan": wf.plan, "report": wf.report}


@router.get("/{workflow_id}/tasks", responses={404: {"model": ErrorOut}})
def tasks(workflow_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> list[dict[str, Any]]:
    return views.tasks(session, views.get_workflow(session, tenant_id, workflow_id))


@router.get("/{workflow_id}/findings", responses={404: {"model": ErrorOut}})
def findings(workflow_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> list[dict[str, Any]]:
    return views.findings(session, views.get_workflow(session, tenant_id, workflow_id))


@router.post("/{workflow_id}/cancel", responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}})
def cancel(workflow_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> dict[str, Any]:
    views.get_workflow(session, tenant_id, workflow_id)
    wf = sup.cancel(session, workflow_id, tenant_id)
    return views.summary(session, wf)
