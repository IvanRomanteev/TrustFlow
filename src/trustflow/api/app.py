"""FastAPI app: workflows, approvals, audit, tools. Synthetic data only."""

import hmac
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
from trustflow.retrieval.corpus import PolicyRetriever
from trustflow.tools.registry import ToolRegistry


class CreateWorkflowRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    requested_by: str = Field(default="demo-user", max_length=100)
    # M2 explicit opt-in to mandatory citations (docs/m2.md).
    require_evidence: bool = False


class ApprovalRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    plan_hash: str = Field(min_length=64, max_length=64)
    reason: str = Field(default="", max_length=500)


class Container:
    """Wires the M0 object graph. One ledger => conservation checks hold."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.persistence = "memory"
        self.recovered_runs: list[str] = []
        self.registry: ToolRegistry
        self.audit: AuditLogger
        self.repo: RunRepository
        if settings.db_path:
            # M3 durable mode (docs/m3.md): SQLite behind the same
            # repository/ledger/audit contracts as the in-memory M1/M2
            # implementations. Crash-stranded runs are recovered
            # verification-only; committed payments never re-execute.
            from trustflow.persistence import (
                PersistentLedger,
                SQLiteAuditLogger,
                SQLiteRunRepository,
                recover_interrupted_runs,
            )

            self.registry = ToolRegistry(ledger=PersistentLedger(settings.db_path))
            self.audit = SQLiteAuditLogger(settings.db_path)
            self.repo = SQLiteRunRepository(settings.db_path)
            self.persistence = "sqlite"
            self.recovered_runs = recover_interrupted_runs(self.repo, self.audit)
        else:
            self.registry = ToolRegistry()
            self.audit = AuditLogger()
            self.repo = RunRepository()
        self.policy = PolicyEngine(settings)
        self.approvals = HumanApprovalService()
        # Explicit fake/live switch; live without MODEL_API_KEY raises here.
        provider = build_provider(settings)
        self.llm_mode = settings.llm_mode
        planner = Planner(provider, self.policy,
                          max_output_tokens=settings.max_output_tokens)
        executor = Executor(self.registry, settings)
        verifier = Verifier(self.registry.ledger, self.registry.ledger.total_balance())
        retriever = PolicyRetriever(
            top_k=settings.retrieval_top_k,
            min_score=settings.retrieval_min_score)
        semantic = SemanticReviewer(
            provider, self.registry.ledger,
            max_output_tokens=settings.max_output_tokens,
            retriever=retriever,
            retrieval_min_score=settings.retrieval_min_score)
        self.engine = WorkflowEngine(
            planner, executor, verifier, self.policy, self.approvals,
            self.audit, self.repo, settings, semantic=semantic,
            retriever=retriever,
        )


@lru_cache
def get_container() -> Container:
    return Container(get_settings())


def create_app(container: Container | None = None) -> FastAPI:
    app = FastAPI(title="TrustFlow", version="0.1.0",
                  description="Self-verifying multi-agent workflows. Synthetic data only.")
    box = container or get_container()
    app.state.container = box
    app.add_middleware(
        CORSMiddleware,
        allow_origins=box.settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Approval-Token"],
    )

    def dep() -> Container:
        return box

    def require_approval_token(
        c: Annotated[Container, Depends(dep)],
        x_approval_token: Annotated[str | None, Header()] = None,
    ) -> None:
        """Approval authorization (M3). Enforced whenever a token is
        configured (the demo always configures one); unset keeps the
        M1/M2 local-test behaviour. The token is never logged."""
        expected = c.settings.approval_token
        if not expected:
            return
        if x_approval_token is None:
            raise HTTPException(
                status_code=401, detail="X-Approval-Token header required")
        if not hmac.compare_digest(x_approval_token, expected):
            raise HTTPException(
                status_code=403, detail="Invalid approval token")

    @app.get("/health")
    def health(c: Annotated[Container, Depends(dep)]) -> dict:
        return {
            "status": "ok",
            "synthetic_only": True,
            "llm_mode": c.llm_mode,
            "planning_model": c.settings.planning_model,
            "review_model": c.settings.review_model,
            "fast_model": c.settings.fast_model,
            "persistence": c.persistence,
            "approval_auth_required": bool(c.settings.approval_token),
        }

    @app.get("/tools")
    def tools(c: Annotated[Container, Depends(dep)]) -> dict:
        return {"tools": c.registry.tool_definitions()}

    @app.post("/workflows", status_code=201)
    async def create_workflow(req: CreateWorkflowRequest, c: Annotated[Container, Depends(dep)]):
        goal = WorkflowGoal(title=req.title, description=req.description,
                            requested_by=req.requested_by,
                            require_evidence=req.require_evidence)
        run = await c.engine.create_workflow(goal)
        return run

    @app.get("/workflows")
    def list_workflows(c: Annotated[Container, Depends(dep)]) -> list[WorkflowRun]:
        return c.repo.list()

    @app.get("/workflows/{run_id}")
    def get_workflow(run_id: str, c: Annotated[Container, Depends(dep)]) -> WorkflowRun:
        run = c.repo.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return run

    @app.post("/workflows/{run_id}/approve",
              dependencies=[Depends(require_approval_token)])
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

    @app.post("/workflows/{run_id}/reject",
              dependencies=[Depends(require_approval_token)])
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
        except TrustFlowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/workflows/{run_id}/verify-retry",
              dependencies=[Depends(require_approval_token)])
    async def verify_retry(run_id: str, c: Annotated[Container, Depends(dep)]):
        """Verification-only recovery: never executes tools or transfers."""
        run = c.repo.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return await c.engine.retry_verification(run)

    @app.post("/workflows/{run_id}/reconcile",
              dependencies=[Depends(require_approval_token)])
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

