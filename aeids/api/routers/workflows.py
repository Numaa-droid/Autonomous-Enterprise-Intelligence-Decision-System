"""Workflows: ask a question, watch the task graph, read verified findings, review (M3, M5)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from aeids.api.deps import SessionDep, TenantDep
from aeids.api.processing import registry, resume_workflow, schedule_workflow
from aeids.api.schemas import ErrorOut
from aeids.orchestration import supervisor as sup
from aeids.orchestration import views
from aeids.reporting import report as report_doc

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
    return {**views.summary(session, wf), "plan": wf.plan, "report": wf.report, "answer": views.answer(wf.report)}


@router.get("/{workflow_id}/tasks", responses={404: {"model": ErrorOut}})
def tasks(workflow_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> list[dict[str, Any]]:
    return views.tasks(session, views.get_workflow(session, tenant_id, workflow_id))


@router.get("/{workflow_id}/findings", responses={404: {"model": ErrorOut}})
def findings(workflow_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> list[dict[str, Any]]:
    return views.findings(session, views.get_workflow(session, tenant_id, workflow_id))


@router.get("/{workflow_id}/report", responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}})
def report(workflow_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> dict[str, Any]:
    """The workflow's checked report (M7): metadata, the content as JSON (schema ``report/1.0``)
    and where to download it as Markdown. 409 until the workflow completes."""
    return report_doc.view(report_doc.ensure(session, views.get_workflow(session, tenant_id, workflow_id)))


@router.get("/{workflow_id}/report.md", response_class=Response,
            responses={200: {"content": {"text/markdown": {}}}, 404: {"model": ErrorOut}, 409: {"model": ErrorOut}})
def report_markdown(workflow_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> Response:
    """The report as a Markdown file: answer, calculations, limitations, review, numbered sources."""
    row = report_doc.ensure(session, views.get_workflow(session, tenant_id, workflow_id))
    name = f"aeids-report-{str(workflow_id)[:8]}-v{row.version}.md"
    return Response(row.markdown, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "ETag": f'"{row.sha256}"'})


class ReviewIn(BaseModel):
    task_id: uuid.UUID
    decision: Literal["approve", "reject", "rerun"]
    note: str = Field(default="", max_length=2000)
    reviewer: str = Field(default="reviewer", min_length=1, max_length=80)


def apply_review(request: Request, session, tenant_id: uuid.UUID, workflow_id: uuid.UUID,
                 body: ReviewIn) -> dict[str, Any]:
    task = sup.review(session, workflow_id, tenant_id, body.task_id, decision=body.decision, note=body.note,
                      reviewer=body.reviewer)
    processing = resume_workflow(request.app.state, workflow_id,
                                 task.id if body.decision == "rerun" else None)
    session.expire_all()
    return {**views.summary(session, views.get_workflow(session, tenant_id, workflow_id)), "processing": processing}


@router.post("/{workflow_id}/reviews", responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}})
def review(workflow_id: uuid.UUID, body: ReviewIn, request: Request, session: SessionDep,
           tenant_id: TenantDep) -> dict[str, Any]:
    """Decide on a task waiting for human review: approve (its open findings become verified),
    reject (they are rejected, the task fails) or rerun (a task without findings runs again,
    e.g. after the missing report was uploaded)."""
    return apply_review(request, session, tenant_id, workflow_id, body)


@router.post("/{workflow_id}/cancel", responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}})
def cancel(workflow_id: uuid.UUID, session: SessionDep, tenant_id: TenantDep) -> dict[str, Any]:
    views.get_workflow(session, tenant_id, workflow_id)
    wf = sup.cancel(session, workflow_id, tenant_id)
    return views.summary(session, wf)
