# Two-minute narrated demonstration

**Format:** nine scenes, exactly 120 seconds of planned screen time. English
narration is 250 words by the packaging word-count check; rehearse to the scene boundaries and leave
short visual pauses. This is a script and an offline presentation, not an already
recorded or hosted video. Use the [storyboard](storyboard.md),
[HTML slides](presentation.html) or [PDF](presentation.pdf).

Keep **“Accepted M5.2 · fake mode · synthetic funds”** visible throughout. Screens
are real archived Chromium captures from canonical replay #1 at accepted SHA
`38cc3942dbd715681d59bfd893e47643210294a9`.

## 1 · 00:00–00:12 · The operational question

An agent says a payment succeeded. Was it authorized? Did it happen once?
TrustFlow makes those questions checkable. Everything here is synthetic; the
accepted demonstration uses deterministic fake inference.

## 2 · 00:12–00:25 · Architecture

The Planner proposes. Deterministic policy decides risk. Humans approve high-risk
actions. Execution preserves operation identity; verification reads ledger
evidence. Muse sits behind the provider boundary. Live results remain pending.

## 3 · 00:25–00:43 · Exact-hash approval

This synthetic five-thousand-dollar transfer pauses. The screen shows the plan
version, payload and full hash. Approval authorizes that exact snapshot. Accepted
checks reject missing credentials, wrong hashes and replayed decisions. The
model cannot grant itself permission.

## 4 · 00:43–00:55 · Verify the outcome

After approval, one transaction commits. Independent SQLite observations match
the authorized operation and conserved total. A separate concurrent approval
test returns one success, one conflict and one transfer.

## 5 · 00:55–01:06 · Rejection stays final

A rejected three-thousand-dollar request creates no transfer. Reload preserves
rejection; late approval cannot revive it. The ledger independently supports the
result.

## 6 · 01:06–01:22 · Timeout and late worker

Controlled fault injection holds a real worker past timeout. After that worker
commits, reconciliation finds the original transaction. An injected fake review
failure leaves verification pending; verification-only retry completes without
another transfer.

## 7 · 01:22–01:41 · SIGKILL after COMMIT

Now the backend is killed after SQLite COMMIT, before saving success. The same
container restarts into verification pending. Verification-only retry preserves
the original transaction, approval hash and snapshot. This proves the controlled
checkpoint, not every possible storage failure.

## 8 · 01:41–01:51 · Missing evidence

Missing mandatory policy evidence fails closed before tools run. This is a local
retrieval check, with no ledger mutation.

## 9 · 01:51–02:00 · Reproduce and inspect

One command repeats six scenarios and publishes evidence. Accepted results are
fake-mode checks. Live Muse evaluation remains pending.

## Recording notes

Use the existing captures as stills; do not present them as a new live session.
For S4/S6 show the controlled-fault label and pair pending with recovered states.
For full-resolution inspection, open the original PNG linked in the storyboard;
avoid a crop that hides the sandbox banner or the approved hash. Add any zoom as
a presentation treatment without editing the evidence file. Runtime duration of
the full replay is distinct from this edited two-minute narration.

Record with a local microphone/editor only when desired. A finished recording,
subtitles and a judge-accessible video URL remain checklist items; no voice
generation or paid media service was invoked for this package.
