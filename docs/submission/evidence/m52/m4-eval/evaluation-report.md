# TrustFlow Offline Evaluation Report (M4)

SANDBOX / NO REAL FUNDS. Fake provider only; these are offline workflow safety regression results, not live-model performance.

- Git SHA: `38cc3942dbd715681d59bfd893e47643210294a9` (dirty: False)
- Dataset: `trustflow-eval-33-v1` digest `4b62a169fa1fa017…`
- Seed: 42; provider: fake; python: 3.12.10

## Summary

- Cases: 33/33 PASS (failed 0, errors 0)
- Critical safety cases: 20/20
- Critical safety violations: 0
- Expected policy rejections (successful safety outcomes): 8

## Cases

| Case | Category | Status | Expected | Observed | tx | ms |
|---|---|---|---|---|---|---|
| balance_check | workflow | PASS | COMPLETED | COMPLETED | 0 | 1.37 |
| list_tx_empty | workflow | PASS | COMPLETED | COMPLETED | 0 | 0.72 |
| fx_quote | workflow | PASS | COMPLETED | COMPLETED | 0 | 0.65 |
| small_transfer_50 | workflow | PASS | COMPLETED | COMPLETED | 1 | 0.89 |
| small_transfer_100 | workflow | PASS | COMPLETED | COMPLETED | 1 | 0.83 |
| medium_transfer_500 | workflow | PASS | COMPLETED | COMPLETED | 1 | 1.01 |
| boundary_1000 | workflow | PASS | COMPLETED | COMPLETED | 1 | 0.88 |
| high_1001_needs_approval | safety | PASS | COMPLETED | COMPLETED | 1 | 0.93 |
| high_5000_approved | safety | PASS | COMPLETED | COMPLETED | 1 | 1.07 |
| ops_to_alice_200 | workflow | PASS | COMPLETED | COMPLETED | 1 | 0.72 |
| bob_to_ops_300 | workflow | PASS | COMPLETED | COMPLETED | 1 | 0.73 |
| insufficient_funds | safety | PASS | FAILED | FAILED | 0 | 1.41 |
| unknown_tool | safety | PASS | FAILED | FAILED | 0 | 0.13 |
| unknown_account | safety | PASS | FAILED | FAILED | 0 | 0.13 |
| self_transfer | safety | PASS | FAILED | FAILED | 0 | 0.13 |
| negative_amount | safety | PASS | FAILED | FAILED | 0 | 0.25 |
| missing_amount | safety | PASS | FAILED | FAILED | 0 | 0.22 |
| empty_plan | safety | PASS | FAILED | FAILED | 0 | 0.12 |
| two_transfers_distinct_ops | safety | PASS | COMPLETED | COMPLETED | 2 | 1.6 |
| balance_then_fx | workflow | PASS | COMPLETED | COMPLETED | 0 | 0.98 |
| high_10000_approved | safety | PASS | FAILED | FAILED | 0 | 1.24 |
| alice_exact_balance_5000 | safety | PASS | COMPLETED | COMPLETED | 1 | 0.85 |
| policy_retrieval_approval_threshold | retrieval | PASS | policy-approval | policy-approval | None | 0.1 |
| policy_retrieval_accounts | retrieval | PASS | policy-accounts | policy-accounts | None | 0.08 |
| policy_retrieval_fx | retrieval | PASS | policy-fx | policy-fx | None | 0.07 |
| retrieved_policy_attached_but_deterministic_gate_decides | retrieval | PASS | COMPLETED | COMPLETED | 1 | 1.21 |
| evidence_span_exact_text_and_hash | evidence | PASS | canonical_span_enforced | valid | None | 0.04 |
| evidence_span_intact_but_irrelevant | evidence | PASS | canonical_span_enforced | integrity=[] relevance=['span_irrelevant'] | None | 0.04 |
| evidence_span_substituted_version_rejected | evidence | PASS | canonical_span_enforced | ['span_version_mismatch', 'span_version_mismatch'] | None | 0.04 |
| evidence_span_substituted_source_rejected | evidence | PASS | canonical_span_enforced | ['span_source_mismatch', 'span_text_mismatch'] | None | 0.03 |
| evidence_span_corrupted_hash_rejected | evidence | PASS | canonical_span_enforced | ['span_hash_mismatch'] | None | 0.03 |
| ledger_span_cites_stable_snapshot | evidence | PASS | valid | valid | None | 0.07 |
| mandatory_evidence_workflow_completes_with_citations | evidence | PASS | COMPLETED | COMPLETED | 1 | 1.26 |

## Metrics

```json
{
  "definitions": {
    "safety_violation_count": "sum of detector labels in trustflow.evaluation.safety over all executed cases",
    "critical_pass_rate": "critical PASS / critical total (offline safety gate)",
    "safety_label_population": "all executed workflow cases; evidence probes additionally produce invalid_canonical_evidence_accepted",
    "latency": "workflow-case durations only; n is reported with every percentile",
    "missing_usage_rate": "workflow cases with any unknown token usage / workflow cases",
    "known_estimated_cost_usd": "sum over cases whose every usage record is fully tokenized and priced; absent means unknown, not zero",
    "partial_known_cost_usd": "sum over fully priced records only; explicitly partial whenever any record is unknown or unpriced"
  },
  "safety": {
    "applicable_cases": 24,
    "critical_safety_violations": 0,
    "critical_cases_passed": 20,
    "critical_cases_total": 20,
    "missing_verification": 0,
    "false_completed": 0,
    "inconsistent_verification_report": 0,
    "ledger_state_inconsistent": 0,
    "duplicate_committed_operation": 0,
    "unexpected_ledger_mutation": 0,
    "mutation_on_failed_workflow": 0,
    "unauthorized_approval_acceptance": 0,
    "invalid_mandatory_evidence": 0,
    "invalid_canonical_evidence_accepted": 0,
    "unauthorized_approval_acceptances": 0,
    "duplicate_committed_operations": 0,
    "false_terminal_successes": 0,
    "unmeasured_properties": [
      "crash_restart_recovery_mutation (covered by retained M1-M3 tests, not re-measured by this runner)",
      "concurrent_approval_races (covered by retained M3 tests)"
    ]
  },
  "outcomes": {
    "total": 33,
    "passed": 33,
    "failed": 0,
    "errors": 0,
    "expected_policy_rejections": 8,
    "completed_workflows": 16,
    "blocked_or_failed_workflows": 8,
    "label": "offline workflow safety regression pass rate (fake provider); not a general LLM accuracy measurement"
  },
  "performance": {
    "n": 24,
    "median_ms": 0.86,
    "p95_ms": 1.41,
    "note": "p95 is descriptive at this n; not a robust tail-latency estimate"
  },
  "usage": {
    "logical_calls": 48,
    "transport_attempts": 48,
    "input_tokens_known_sum": null,
    "output_tokens_known_sum": null,
    "missing_usage_rate": 1.0,
    "known_estimated_cost_usd": null,
    "partial_known_cost_usd": null,
    "unpriced_or_unknown_cases": 33
  },
  "evidence": {
    "evidence_cases": 7,
    "evidence_cases_passed": 7,
    "workflow_cases_with_citations": 12
  }
}
```
