# M5.3 live results: one verified Spark planning diagnostic

**One real read-only planning inference succeeded. Full live end-to-end
acceptance and actual Wonder inference remain PENDING.** No new paid request
was performed or authorized during final integration.

## Verified experiment

Source: local sanitized diagnostic report and its execution record, dated
2026-11-04. The public [report projection](../release/evidence/spark-planning.json)
retains measured results while removing request/response/workflow identifiers.
It records the original report's SHA-256
`e6727630ca5dd423efdb8bc43bd94d18a144c3e56e5a09bb2cbda0ca95f220ea`;
it is explicitly a derived projection, not a byte-exact original.

| Field | Observed value |
|---|---|
| Exact source | `c48c2affd3dda652e00aca9f690404c82182b694` |
| Git tree | `25935582bacfceea40890a3e0d2bfd4aa214bf7d` |
| Model | `muse-spark-1.3` |
| HTTP / response | 200 / completed |
| Attempts / transport retries / repair | 1 / 0 / 0 |
| Output cap / reasoning effort | 4096 / low |
| Typed plan / fixed read-only goal | valid / matched |
| Tokens | 191 input / 974 output |
| Reasoning tokens | 893, already included in output |
| Estimated charge | $0.00437825; not a provider invoice |
| Latency | 9868.45 ms for this one request; not a benchmark |
| Tools / transfers | 0 / 0 |
| Ledger | Unchanged, integrity ok, synthetic total 16,200 USD |
| Execution process | exit 0; `live_integration_pass=false` |

The reported actual response shape includes a completed reasoning item and one
completed assistant message with `output_text`. Only bounded allowlisted metadata
is retained; prompts, generated text, raw bodies and private reasoning are not
published. The provider's real typed plan validation and deterministic policy
accepted the proposal, but no plan step executed.

## Earlier failed attempts and interpretation

The earlier report records two HTTP 200 planning failures at output cap 1200:
594 input / 2400 output tokens in total, estimated charge $0.0109425. Both
responses failed validation, and no transfers occurred. The report lacked
status/incomplete-reason/reasoning metadata, so the precise failure cause cannot
be reconstructed. It is consistent with output-budget exhaustion but does not
prove it.

The later success changed both cap and effort. Because those changes were
combined and generation is stochastic, neither individual causal effect is
established. The successful request used fewer than 1200 output tokens under low
effort. Do not claim that increasing the cap alone fixed the earlier responses.
[Original diagnostic procedure](../m53-live.md).

## Remaining gates

- Multi-scenario real Spark execution with HIGH approval, rejection, ledger
  effects, semantic negative cases and verification-only recovery.
- Real Wonder requests and mixed-profile behavior.
- Independently supported live schema/error/budget/failure-accounting outcomes
  across the matrix; repeatability, quality, latency and cost comparisons.
- Any additional paid experiment requires a new explicit owner authorization
  with agreed scope/limits. The single-request authorization is exhausted.

The Chromium demo/video is **FakeLLMProvider M5.2 evidence**, including controlled
timeout and SIGKILL. It supplies no real Muse inference evidence. The historical
offline M5.3 foundation has the limited `M53-E1-EXCEPTION-01` disposition documented
in [release provenance](../release/final-integration.md); it is not unconditional
live acceptance and does not transfer to this new merged candidate.
