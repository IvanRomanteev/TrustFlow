# TrustFlow: make agent actions independently checkable

## One-line pitch

TrustFlow combines hash-bound human approvals, stable payment intents and
ledger-grounded verification to make synthetic agent workflows inspectable,
including after timeouts and process interruption.

## The problem

A convincing agent response leaves three operational questions unanswered:
Was this exact action authorized? Did it happen once? Can we establish the
outcome after a timeout? A worker can commit a transfer after its caller stops
waiting, or the process can die between COMMIT and a saved success result.
Retrying the original instruction can then produce another payment.

TrustFlow explores this problem in a synthetic financial sandbox, where the
operation, authorization and evidence can be inspected without moving real funds.

## The solution

A Planner translates a task into typed tool steps. A deterministic PolicyEngine
classifies risk. HIGH-risk plans stop at Human Approval, where the operator sees
the exact version, operation payload and SHA-256 hash. Approval freezes an
isolated snapshot and authorizes its payment intents. The Executor checks those
intents at the mutation boundary and uses their stable identities as idempotency
keys. The Verifier reads ledger evidence independently of execution claims.

After a timeout or post-COMMIT crash, TrustFlow preserves the operation identity,
exposes the uncertain or pending state, and reconciles the existing transaction.
Verification-only retry checks the result without issuing another transfer.

## Technical novelty in this prototype

The contribution connects authorization, execution identity and independent
evidence across failure boundaries:

- A plan hash binds a decision to a versioned snapshot; it does not replace
  authentication. Execution also checks authorized operation payloads.
- Backend-owned payment intents preserve logical identity across re-planning
  and recovery. A new step number cannot silently authorize a new transfer.
- Verification checks committed rows against authorized operations, intent
  coverage and conservation. Semantic PASS cannot override deterministic FAIL.
- Real-worker timeout and post-COMMIT SIGKILL experiments expose the intermediate
  state, original `tx_id`, recovery and absence of another transfer.
- Reproducibility compares stable outcomes while checking raw approval,
  snapshot and evidence identities separately within each invocation.

This is an engineering contribution demonstrated by the sandbox, not a new
cryptographic primitive or universal exactly-once delivery guarantee.
[Mechanisms and source references](safety.md).

## Why Meta Muse matters

Muse supplies the reasoning interface this architecture is designed to bound:
natural language → structured plan, and goal + approved plan + ledger evidence →
semantic review. Meta describes Muse Wonder and Muse Spark as models for
agentic workflows with tool calling and structured output.
[Meta model documentation](https://dev.meta.ai/docs/models).

The accepted source contains `MetaResponsesProvider` using the Responses API,
strict flat plan/review schemas and fail-closed error paths. Its default planning
and review model is `muse-wonder-1.0`; `muse-spark-1.3` is configured as the fast
model. Actual routing, availability and quality must be recorded in a verified
M5.3 run. Configured names alone are not live integration evidence.

Our design hypothesis is that explicit plan and review contracts make Muse
outputs useful for auditable workflows. That hypothesis remains unmeasured with
live inference. The screenshots and accepted safety results use deterministic
fake output; **no live-model score, latency, cost or comparison is claimed**.
[Pending live results](m53-live-results.md).

## What we can demonstrate now

Independent M5.2 acceptance for
`38cc3942dbd715681d59bfd893e47643210294a9` closes S1–S6: balance inspection,
exact-hash approval and a concurrent decision race, durable rejection, a late
worker commit, mandatory-evidence failure and SIGKILL recovery. Two canonical
replays and two maintained opt-in replays each completed all six scenarios.
Four unique transfers per canonical replay conserved the synthetic total of
36,200 USD; independent SQLite observations supported the verdict.
[Evidence and limitations](evidence-status.md).

## Competition positioning and boundaries

Suggested theme: **Build for Your World**, applied to controlled operational
workflows. This is a proposed fit; the entrant must confirm track and current
portal requirements. Meta lists this theme and Push the Frontier on the
[official event page](https://dev.meta.ai/events/global-hackathon).

This is a trusted local, single-instance SQLite prototype with a shared demo
token. Production identity/RBAC, real banking integration, multi-host operation,
container recreation durability and live inference evaluation remain outside the
accepted evidence. The package provides a reproducible local demonstration and
an honest path to measuring Muse integration.
