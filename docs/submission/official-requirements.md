# Official requirement check · 3 November 2026

## Confirmed from public organizer pages

The published project deadline is **4 November 2026, 21:00 ET**, equivalent to
**5 November, 07:00 Asia/Qyzylorda**. Organizers reserve the right to change dates.
The event offers indie-developer and startup tracks and the themes Build for Your
World and Push the Frontier. Confirm the entrant's actual track and eligibility
through the authorized account; this package makes no eligibility determination.
[Meta event page](https://dev.meta.ai/events/global-hackathon),
[Cerebral Valley event page](https://cerebralvalley.ai/e/meta-ai-developer-hackathon).

Meta's page describes building with its AI products, including Muse. That is
context for the project, not independent evidence that TrustFlow has passed live
model evaluation. The package's model-usage statement distinguishes implemented
integration from independently accepted fake-mode results.
[Official event description](https://dev.meta.ai/events/global-hackathon).

## What could and could not be inspected

| Public route | Result of read-only inspection on 2026-11-03 |
|---|---|
| Meta event page | Public deadline, tracks, themes and link to the waiver are readable. |
| Cerebral Valley event page | Public event details are readable. A generic “Event Closed” label is also present; it does not establish the deadline of an authenticated project form. |
| [Project entry](https://cerebralvalley.ai/e/meta-ai-developer-hackathon/hackathon/submit) | HTTP 200; event details and a Submit Project entry/modal are visible. The anonymous session did not expose project fields, field limits or upload limits. No submission action was invoked. |
| [Linked waiver](https://cerebralvalley.ai/e/meta-ai-developer-hackathon/waiver) | HTTP 200 but only a Loading shell was readable in Chromium. Complete rules could not be verified from that response. |
| `/submit` and `/rules` directly below the event slug | HTTP 404 in the local read-only browser check; these are not usable requirement sources. |

The public pages say that selected participants must accept Official Rules.
The authenticated form and complete rules remain a final manual check. Do not
claim that a GitHub link, video length, file format or description limit is an
official requirement based on this package alone.
[Organizer event page](https://cerebralvalley.ai/e/meta-ai-developer-hackathon).

## Prepared formats requested by the project owner

These are **our preparation targets, not verified portal constraints**:

- Video: 120 seconds, with English narration/captions and explicit static-capture labels.
- Short summary: at most 500 characters.
- Full description: 1,500–2,500 characters.
- Title, GitHub URL, video URL placeholder, technology stack, Meta usage,
  architecture, technical novelty, reproducibility, setup and limitations.

[Copy-ready fields](submission-text.md) and [JSON values](submission-fields.json)
can be adapted without inventing portal fields. [Final integration steps](integration.md)
keep account/form checks and publication separate from this local package.

## Repository access check

The connected GitHub read tools confirmed
[IvanRomanteev/TrustFlow](https://github.com/IvanRomanteev/TrustFlow) is public.
The published `m5.2/reproducible-demo` branch points to accepted SHA
`38cc3942dbd715681d59bfd893e47643210294a9`.
[Accepted source permalink](https://github.com/IvanRomanteev/TrustFlow/tree/38cc3942dbd715681d59bfd893e47643210294a9).
The remote branch listing did not contain `m5.4/hackathon-submission` at check
time. The final M5.4 commit and video are local; publication links remain pending.
No repository write, push or remote-ref update was performed.
