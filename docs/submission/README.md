# TrustFlow · M5.4 submission package

**A reviewable competition package built on independently accepted M5.2.**
New narrative and presentation material is English. Original evidence retains
its source language and exact bytes.

| Material | Artifact |
|---|---|
| Overview and setup | [Repository README](../../README.md) |
| Problem, solution, novelty and Muse fit | [Pitch](pitch.md) |
| Timed English narration | [Two-minute script](demo-script.md) |
| Scenes, screenshots and evidence anchors | [Storyboard](storyboard.md) |
| Offline slides and handout | [HTML](presentation.html), [PDF](presentation.pdf) |
| Diagram and source map | [Architecture](architecture.md), [SVG](architecture.svg), [Mermaid](architecture.mmd) |
| Hashes, operation identity, grounding, recovery | [Safety](safety.md) |
| Accepted fake results and pending gates | [Evidence status](evidence-status.md) |
| Future verified live results | [M5.3 template](m53-live-results.md) |
| Submission readiness | [Checklist](checklist.md) |
| Primary source and archival scope | [Evidence guide](evidence/README.md), [provenance](evidence/provenance.json) |
| Packaging checks | [Validation](validation.md) |

Base: `38cc3942dbd715681d59bfd893e47643210294a9`.
Branch: `m5.4/hackathon-submission`.
The exact package commit is reported in the delivery message and obtained with
`git rev-parse HEAD`; an artifact cannot contain its own future Git SHA. Its
parent must equal the base above. Independent M5.4 acceptance is pending;
packaging does not extend the M5.2 verdict to live inference.

Open `presentation.html` directly in a browser. It uses local files and system
fonts, has keyboard navigation (Left/Right, Home/End), and provides a timed
120-second autoplay. It presents archived screenshots, not a recording of a new
execution. `presentation.pdf` contains the same nine scenes. No generated UI
image, external CDN, model call or hosted service is needed to view it.

The repository has no configured remote at packaging time. Repository, video and
submission URLs remain explicit `PENDING` fields in the checklist. No push,
merge, deployment, submission or accepted-tag change is part of this package.
