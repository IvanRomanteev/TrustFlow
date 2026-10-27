"""FastAPI app: workflows, approvals, audit, tools. Synthetic data only."""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from trustflow.agents.approval import HumanApprovalService
from trustflow.agents.executor import Executor
from trustflow.agents.planner import Planner
from trustflow.agents.semantic import SemanticReviewer
from trustflow.agents.verifier import Verifier
from trustflow.audit.logger import AuditLogger
from trustflow.core.errors import ApprovalError, TrustFlowError
from trustflow.core.settings import Settings, get_settings
from trustflow.domain.models import WorkflowGoal, WorkflowRun
from trustflow.llm.provider import build_provider
from trustflow.orchestration.engine import RunRepository, WorkflowEngine
from trustflow.policies.policy_engine import PolicyEngine
from trustflow.tools.registry import ToolRegistry


class CreateWorkflowRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    requested_by: str = Field(default="demo-user", max_length=100)


class ApprovalRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    plan_hash: str = Field(min_length=64, max_length=64)
    reason: str = Field(default="", max_length=500)


class Container:
    """Wires the M0 object graph. One ledger => conservation checks hold."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = ToolRegistry()
        self.policy = PolicyEngine(settings)
        self.audit = AuditLogger()
        self.repo = RunRepository()
        self.approvals = HumanApprovalService()
        # Explicit fake/live switch; live without MODEL_API_KEY raises here.
        provider = build_provider(settings)
        self.llm_mode = settings.llm_mode
        planner = Planner(provider, self.policy,
                          max_output_tokens=settings.max_output_tokens)
        executor = Executor(self.registry, settings)
        verifier = Verifier(self.registry.ledger, self.registry.ledger.total_balance())
        semantic = SemanticReviewer(
            provider, self.registry.ledger,
            max_output_tokens=settings.max_output_tokens)
        self.engine = WorkflowEngine(
            planner, executor, verifier, self.policy, self.approvals,
            self.audit, self.repo, settings, semantic=semantic,
        )


@lru_cache
def get_container() -> Container:
    return Container(get_settings())


def create_app(container: Container | None = None) -> FastAPI:
    app = FastAPI(title="TrustFlow", version="0.1.0",
                  description="Self-verifying multi-agent workflows. Synthetic data only.")
    box = container or get_container()
    app.state.container = box

    def dep() -> Container:
        return box

    @app.get("/health")
    def health(c: Annotated[Container, Depends(dep)]) -> dict:
        return {
            "status": "ok",
            "synthetic_only": True,
            "llm_mode": c.llm_mode,
            "planning_model": c.settings.planning_model,
            "review_model": c.settings.review_model,
            "fast_model": c.settings.fast_model,
        }

    @app.get("/tools")
    def tools(c: Annotated[Container, Depends(dep)]) -> dict:
        return {"tools": c.registry.tool_definitions()}

    @app.post("/workflows", status_code=201)
    async def create_workflow(req: CreateWorkflowRequest, c: Annotated[Container, Depends(dep)]):
        goal = WorkflowGoal(title=req.title, description=req.description,
                            requested_by=req.requested_by)
        run = await c.engine.create_workflow(goal)
        return run

    @app.get("/workflows/{run_id}")
    def get_workflow(run_id: str, c: Annotated[Container, Depends(dep)]) -> WorkflowRun:
        run = c.repo.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return run

    @app.post("/workflows/{run_id}/approve")
    async def approve(run_id: str, req: ApprovalRequest, c: Annotated[Container, Depends(dep)]):
        run = c.repo.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        try:
            c.approvals.approve(run, req.actor, req.plan_hash, req.reason)
            c.audit.log(run_id, req.actor, "approval_granted", {"plan_hash": req.plan_hash})
            return await c.engine.resume_after_approval(run)
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TrustFlowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/workflows/{run_id}/reject")
    def reject(run_id: str, req: ApprovalRequest, c: Annotated[Container, Depends(dep)]):
        run = c.repo.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        try:
            c.approvals.reject(run, req.actor, req.plan_hash, req.reason)
            c.audit.log(run_id, req.actor, "approval_rejected", {"plan_hash": req.plan_hash})
            return c.repo.save(run)
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/workflows/{run_id}/verify-retry")
    async def verify_retry(run_id: str, c: Annotated[Container, Depends(dep)]):
        """Verification-only recovery: never executes tools or transfers."""
        run = c.repo.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return await c.engine.retry_verification(run)

    @app.post("/workflows/{run_id}/reconcile")
    async def reconcile(run_id: str, c: Annotated[Container, Depends(dep)]):
        """Resolve uncertain execution outcomes from workers/ledger only."""
        run = c.repo.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return await c.engine.reconcile(run)

    @app.get("/workflows/{run_id}/audit")
    def audit(run_id: str, c: Annotated[Container, Depends(dep)]) -> dict:
        if c.repo.get(run_id) is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return {"events": [e.model_dump(mode="json") for e in c.audit.list(run_id)]}

    return app


app = create_app()

