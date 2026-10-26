# TrustFlow Architecture (M0)

## Loop

Goal -> Planner (LLM proposes flat steps) -> PolicyEngine (deterministic risk)
-> [HIGH risk? pause AWAITING_APPROVAL] -> HumanApprovalService (hash-bound,
single-use) -> Executor (idempotent tools, succeeded steps skipped)
-> Verifier (all-steps, no-failures, ledger conservation, tx evidence)
-> COMPLETED | NEEDS_REVISION (bounded re-plan, new plan version) | FAILED.

## Components

- `Planner` (`agents/planner.py`): goal to typed `Plan`; never sets final risk.
- `Executor` (`agents/executor.py`): per-step timeout, stable idempotency keys.
- `Verifier` (`agents/verifier.py`): independent deterministic checks.
- `PolicyEngine` (`policies/policy_engine.py`): backend risk + approval gating.
- `HumanApprovalService` (`agents/approval.py`): approve/reject bound to
  `plan.version` + `plan.plan_hash`; repeats and stale hashes raise `ApprovalError`.
- `ToolRegistry` (`tools/registry.py`): allowlist + Pydantic arg validation;
  emits flat, non-recursive JSON Schemas for the Meta Model API.
- `WorkflowEngine` (`orchestration/engine.py`): validated state transitions,
  `max_steps` / `max_retries` / timeouts, in-memory `RunRepository` (M1: DB).
- `AuditLogger` (`audit/logger.py`): append-only hashed events, secrets redacted.
- `LLMProvider` (`llm/provider.py`): `FakeLLMProvider` for deterministic tests;
  `MetaResponsesProvider` calls the Responses API with `muse-wonder-1.0` for
  planning/verification and configurable `muse-spark-1.3` for cheap inference.

## State machine

`CREATED -> PLANNING -> PLANNED -> AWAITING_APPROVAL|EXECUTING -> VERIFYING ->
COMPLETED|NEEDS_REVISION|FAILED`, plus `REJECTED`/`CANCELLED`. Terminal states
reject all further transitions (`domain/state_machine.py`).

## Next (M1)

React dashboard over this API, persistent run store, real Muse inference
behind `LLMProvider`, streaming progress, richer synthetic fraud scenarios.
