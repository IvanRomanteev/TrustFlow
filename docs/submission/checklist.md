# Submission checklist

This is a prepared documentation/presentation package, not an assertion that a
portal submission is complete. Checkmarks below refer to material included here;
independent M5.4 acceptance and external publication remain open.

## Repository and identity

- [x] Package based directly on accepted M5.2 SHA
  `38cc3942dbd715681d59bfd893e47643210294a9` in branch
  `m5.4/hackathon-submission` and a separate worktree.
- [x] English README, pitch, architecture, safety explanation, storyboard,
  timed narration and offline HTML/PDF slides are prepared.
- [x] Independent M5.2 report and original screenshot/evidence copies retain
  exact bytes with a provenance manifest; original accepted README is retained.
- [x] Repository URL: [IvanRomanteev/TrustFlow](https://github.com/IvanRomanteev/TrustFlow),
  confirmed public on 2026-11-03. Accepted M5.2 branch/SHA is published.
- [ ] Judge-accessible branch/commit permalink: **PENDING — local commit only;
  exact SHA is in the delivery message and `git rev-parse HEAD`.**
- [ ] Confirm entrant/team identity, chosen track/theme, applicable license and
  rights to code and materials. No license or eligibility is inferred here.
- [ ] Independent review of the final M5.4 commit and claim boundaries.

No push, merge, deploy, tag creation/move or portal submission was performed.
Publishing links requires the appropriate later authorization.

## Demonstration and required material preparation

- [x] [Two-minute narration](demo-script.md) and [scene storyboard](storyboard.md)
  use real M5.2 Chromium captures, with fake/synthetic labels throughout.
- [x] [Offline HTML deck](presentation.html), [PDF](presentation.pdf) and
  [architecture graphic](architecture.svg) are available locally.
- [x] S4 timeout/late worker and S6 post-COMMIT SIGKILL are explicitly labelled
  controlled fault injection; the S4 semantic failure is explicitly injected.
- [x] Export [local MP4](video/trustflow-demo.mp4): 120 seconds, 1920×1080,
  24 fps, H.264/AAC; static screenshots with labelled editorial zooms, not UI footage.
- [x] Add local English synthetic narration and 30 matching captions; retain
  [transcript](video/transcript.md), [SRT](video/captions.srt) and [WebVTT](video/captions.vtt).
- [x] Verify full audio/video decode, readable hash/approval/transaction/ledger/
  recovery crops, stream timing and caption track. See [finish validation](validation-finish.md).
- [ ] Final human listening review and authorized video-host publication.
- [ ] Public video URL: **PENDING**. Verify judges can access the processed upload.
- [ ] Complete portal-specific summary, team fields, attachments and consent
  requirements after inspecting the current submission form.

The two-minute format is the owner's target, not a verified official rule. The
anonymous project-entry page is readable, but authenticated fields and upload
limits remain unverified. [Public requirement check](official-requirements.md).
The [exact submission copy](submission-text.md) includes a 365-character summary
and a 2,246-character full description, matching the owner's preparation targets.

## Meta integration and results

- [x] The existing `MetaResponsesProvider`, Responses endpoint, strict plan/review
  schemas and model defaults are described with source references.
- [x] Accepted fake-mode workflow results are separated from pending live Meta
  inference throughout README, pitch, slides and result tables.
- [x] [M5.3 template](m53-live-results.md) contains only pending fields for actual
  future model/config/usage/latency/cost/evidence and independent verdict.
- [ ] Independently accept M5.3 offline readiness on its exact candidate.
- [ ] Obtain separate explicit authorization before any paid live experiment.
- [ ] Add actual live evidence and independent verdict if the submission claims
  live integration or model evaluation. Fake results do not close this gate.

## Reproducibility and limitations

- [x] Setup and canonical one-command replay match the accepted runner interface;
  prerequisites, dynamic loopback ports, fresh storage, fault injection and
  automatic owned-resource teardown are explained.
- [x] Two full canonical accepted evidence bundles, including manifests,
  completion markers and all 28 original PNGs, are included.
- [x] Independent ledger observations and normalized comparison are linked.
- [x] Offline M4 33/33 is labelled workflow regression evidence, not model quality.
- [x] Local single-instance SQLite, shared demo token, no production RBAC,
  real banking, public deployment, universal crash/race or container recreation
  guarantee are stated.
- [ ] Reproduce the M5.4 README from a clean machine and retain command/evidence
  logs. The historical M5.2 acceptance is supporting evidence, not this new run.
- [ ] Confirm all artifact URLs and the final SHA after authorized publication.

M5.4 assembly and video work did not execute Docker, cleanup, application tests
or model calls. Validation covers documentation, evidence, local media synthesis,
export, decoding and playback. [Initial checks](validation.md),
[finish checks](validation-finish.md).

## Current organizer information and final portal check

Meta's event page lists the project deadline as **4 November 2026, 9:00 PM ET**
(**5 November 2026, 07:00 Asia/Qyzylorda**), subject to organizer changes.
Rechecked 2026-11-03. Recheck before submitting.
[Official event page](https://dev.meta.ai/events/global-hackathon).

- [x] Review publicly available event information and submission entry on 2026-11-03;
  record accessible facts and unknown constraints in [official-requirements.md](official-requirements.md).
- [ ] Check complete official rules, selected-participant access, submission
  deadline and portal-specific requirements through the organizer.
- [ ] Confirm which prepared files and links the portal actually requires;
  do not infer compliance from this internal preparation checklist.
- [ ] Review final materials for secrets/credentials, unsupported live claims,
  and inaccessible local paths before publication.
- [ ] Submit only through the authorized account/workflow, then retain the actual
  receipt, submitted URLs, timestamp and exact source commit.

Submission URL / receipt: **PENDING**. Public deployment: **none claimed**.

Follow [integration.md](integration.md) for the final account, publication,
commit-link and manual submission steps. M5.3 evidence placeholders are preserved.
