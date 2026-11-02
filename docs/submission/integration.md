# Final integration and submission instructions

The package is local and reviewable. It contains a verified narrated MP4 and
copy-ready English fields. No step below has been executed merely because it is
listed. Public publication and submission require separate authorization.

## 1. Inspect the exact local candidate

Worktree:
`C:/Users/vanya/Projects/TrustFlow-context/m54-hackathon-submission/worktree`.
Branch: `m5.4/hackathon-submission`.
This follow-up starts at `cc39725e77ce12e9bba971709bc065c35d27fe42` and adds one
focused commit. The accepted runtime remains
`38cc3942dbd715681d59bfd893e47643210294a9`; no M5.3 commit is integrated.

```powershell
git branch --show-current
git rev-parse HEAD
git status --porcelain=v1
git diff --exit-code 38cc3942dbd715681d59bfd893e47643210294a9 -- src frontend tests scripts Dockerfile docker-compose.yml pyproject.toml
```

Use the exact delivered SHA for review and later publication. Read
[finish validation](validation-finish.md), inspect the
[video](video/trustflow-demo.mp4) with sound, and compare the
[final transcript](video/transcript.md) and [submission fields](submission-text.md).
No claim of a new clean-machine application run follows from video export.

## 2. Complete the account/form check

The entrant should open the authenticated organizer form and inspect the complete
Official Rules. Confirm track/team/eligibility, required fields, length limits,
video hosting/file constraints and any license/disclosure requirements. The
public inspection in [official-requirements.md](official-requirements.md) does
not close this gate. The project's 120-second / 500-character / 1,500–2,500-character
formats are preparation targets and can be adapted to verified constraints.

## 3. Publish only after authorization

The repository [IvanRomanteev/TrustFlow](https://github.com/IvanRomanteev/TrustFlow)
is public and contains accepted M5.2. The remote M5.4 branch was absent during
this check. After explicit authorization, publish the exact reviewed local
M5.4 branch/commit through the agreed Git workflow. Do not move accepted tags or
merge M5.3 as part of that action. Verify that the final commit permalink and
all README/media links are accessible anonymously.

Upload `video/trustflow-demo.mp4` to the chosen judge-accessible video host only
after authorization. Use `video/captions.srt` when the host supports captions.
Check the processed upload with sound, seek through the hash/ledger/recovery
details, verify duration and select an access setting that allows judges to view
without requesting permission. Retain the actual URL and uploaded file hash.

Update only confirmed publication values: `demo_video_url` and
`final_m54_commit_url` in `submission-fields.json` and the corresponding Markdown
fields/checklist. These are intentionally `PENDING` in the local package.

## 4. Integrate live evidence only when independently supported

The video always represents accepted **fake-mode M5.2**. A later M5.3 developer
report or branch head does not convert its footage into live inference evidence.
Populate [m53-live-results.md](m53-live-results.md) only with actual sanitized
results, exact candidate/configuration, usage including failures, ledger proof
and an independent verdict. Record the separately authorized integration range.
Do not relabel this video or overwrite the accepted captures to imply a live run.

## 5. Submit manually and retain the receipt

After the final account, link and claim checks, copy the approved text into the
actual form and attach the verified repository/video links. The owner performs
the final submission separately. Retain the submitted text, URLs, source SHA,
timestamp and organizer receipt. This package neither submits nor asserts
compliance with unread authenticated rules.
