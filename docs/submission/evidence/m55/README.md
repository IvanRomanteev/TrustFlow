# M5.5 minimal sanitized evidence

Historical session: **2026-11-04**, application source
[`aacfed074a4116dbded98ee88f783226a910e55c`](https://github.com/IvanRomanteev/TrustFlow/tree/aacfed074a4116dbded98ee88f783226a910e55c),
tree `89eb3dbb4c79ad11e01490e1404614a573734703`.
This package projects retained records; it does not make new API requests.

| Scope | Retained observation |
|---|---|
| Spark matrix | 9 calls; read, approve, reject, recovery and semantic-negative cases: 5/5 |
| Separate Spark recovery probe | 1 semantic review; offline-prepared plan and pre-inference outage fixture |
| Wonder recovery probe | **1 semantic review only**, using `muse-wonder-1.0`; **offline-prepared plan**, local pre-inference outage fixture |
| Models | 10 `muse-spark-1.3` + 1 `muse-wonder-1.0`; HTTPTransport witnesses record matching response models |
| Transport | 11 HTTP 200 / completed responses, first transport attempt; no transport retries or planning repairs |
| Accounting | 11,042 input + 11,299 output tokens; **$0.073201 estimated**, not an invoice |
| Historical rates per million input/output tokens | Spark $1.25/$4.25; Wonder $4/$20; retained experiment assumptions, not a current price quote |
| Bounds | 11 requests, $0.33 advisory budget, max output 4096, reasoning effort low |
| Synthetic SQL | Three independently read SQLite files; unique operations, exact approved canonical hashes, $1,500 approved and $1,200 recovered transfers in matrix; $1,200 in each separate probe |

The Spark matrix recovery deliberately replaced its first real semantic review
with `INVALID` after inference, then made a separate verification-only call. This
is injected failure evidence, not a naturally occurring provider outage. Approval
decisions were automated fixtures. Interactive human decisions and actual worker
timeout/SIGKILL are exercised separately with FakeLLMProvider in Chromium S1–S6.
The separate recovery probes preserve the same ledger before/after review.

## Verify from this checkout

No dependencies or credentials are needed (Python 3.12+):

```powershell
python scripts/m55_public_evidence.py
```

The verifier checks file hashes, models, statuses, unique response fingerprints,
token/cost totals, synthetic conservation, approval hashes and operation/payload
bindings. It rejects optimized Python (`-O`). `evidence.json` contains:

- `stages[].calls`: allowlisted accounting and wire model metadata. Provider IDs
  are SHA-256 fingerprints; response bodies, prompts and reasoning are absent.
- `stages[].sqlite`: read-only SQL projections, account IDs/balances/currency,
  transactions and minimal run/approval records. Canonical synthetic plan fields
  are retained because the exact plan hash cannot be reproduced without them;
  rationale, owner names, descriptions, audit prose and semantic summaries are removed.
- `stages[].cases`: selected historical case observations and ledger snapshots.
- `sources_sha256`: relative source filenames and SHA-256 hashes, including runner
  seals, reports, attempts, model witnesses and the original SQLite files.

`manifest.json` hashes the public evidence, this guide and the extractor/verifier.
Extraction uses explicit field selection, not generic recursive redaction. Matrix
calls and witnesses are paired by their retained sequence; the wire witness does
not itself carry a response ID. Original files are never rewritten. The SQLite
reader refuses nonempty WAL and uses `mode=ro&immutable=1` plus `integrity_check`.

An authorized reviewer with the retained source directory can also reproduce the
entire projection, check runner seals and compare it exactly:

```powershell
python scripts/m55_public_evidence.py --source C:\path\to\m55-live-video-20261104
```

## Limits and provenance

These are locally retained observations, **not provider-signed receipts**. A
public-only reviewer can recompute the published arithmetic and SQL/hash bindings;
the original-source comparison requires private retained files. Hashes establish
consistency, not authenticity against the artifact owner. Public SQL is a selected
JSON projection, not a complete database or an independently timestamped snapshot.

The external runners are identified by their original hashes; they are not added
as a new live execution path. The application SHA predates this frontend-only
runtime remediation. The package does **not** establish live Wonder planning, a
full Wonder/mixed-model matrix, exhaustive concurrency correctness, account-wide
usage attribution, or full independent M5.5 acceptance. Earlier unsuccessful live
experiments remain in retained historical evidence and are not replaced by this
successful-session projection. No new paid calls were made for remediation.
