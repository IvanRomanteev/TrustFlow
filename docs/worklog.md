# TrustFlow Worklog

## M0 — Walking skeleton (860ce7d..7e744d3)
Planner/Executor/Verifier loop, hash-bound approvals, synthetic ledger,
23 offline tests. Pushed at the time with approval.

## M1 — Real Meta intelligence (7e744d3..4d2bc2d)
Live Responses API provider (Wonder plan + semantic review, Spark config),
validated plans with one repair, usage/cost records, 22 eval scenarios,
59 offline tests, bounded live E2E ($0.036 for the successful run).
Independent review then reproduced seven findings; M1 was not accepted.

## M1 stabilization (this candidate, after 4d2bc2d)
Finding-by-finding fixes, each with a regression test in
`tests/unit/test_m1_stabilization.py` (see docs/m1.md for the table):
operation identities are backend UUIDs retained by payload fingerprint +
occurrence ordinal; revised plans reconcile committed ledger evidence into
current-step results and refresh reads; timeouts produce EXECUTION_UNCERTAIN
with tracked workers and ledger reconciliation (no definitive failure, no
replacement while unresolved); semantic output is validated, the
deterministic report persists first, and failures land in
VERIFICATION_PENDING with verification-only retry (committed transfers are
never rolled back or re-executed); usage is attributed per workflow at call
time with pre-call reservation, unknown tokens stay unknown, and over-budget
records are preserved; LLM mode is a validated fake|live contract; plan
schema covers fx fields; errors/audit payloads are recursively sanitized;
approved plans execute from an isolated deep snapshot; the verifier matches
complete operations against the ledger itself.
Offline only: 75 tests, ruff and mypy clean. No paid API calls were made.
Status: candidate for independent re-review (not self-accepted).
