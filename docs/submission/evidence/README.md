# Evidence provenance and archival scope

Primary source: the independent acceptance archive at
`C:/Users/vanya/Projects/TrustFlow-context/review-m52-38cc394-acceptance`.
This package copies evidence; it does not rerun the original experiments or
modify their outputs. [English results guide](../evidence-status.md).

| Included | Purpose |
|---|---|
| [REVIEW.original.txt](m52/REVIEW.original.txt) | Exact original Russian/English independent report; accepted SHA, limits and verdict |
| [README.m52-original.txt](m52/README.m52-original.txt) | Original accepted README checkout bytes, preserved before its presentation rewrite |
| `m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/` | Complete first canonical published evidence: manifest, completion marker, 14 original PNGs, API/browser/audit/runtime/fault/ledger observations and exact recorded commands |
| `m52/runs/replay-2/075897be048d47229697cab98e1d7de2/` | Complete second canonical published evidence and comparison |
| `m52/independent-sqlite/` | Two independent read-only SQL observations; no database binary |
| `m52/logs/` | Selected independent gate, count, consistency, failure and preservation summaries |
| `m52/m4-eval/` | Published offline 33-case regression report, traces, manifests, repeats and ablations |
| [provenance.json](provenance.json) | Every copied file's source-relative path, byte length and SHA-256 |

`REVIEW.original.txt` is a verbatim archival text, not a rewritten report. Its
original relative links refer to the full source archive, including evidence not
copied here. The `.txt` extension preserves that distinction. The opt-in replay
bundles, SQLite backups, detailed daemon-event logs, preliminary failed observer
attempts and unselected diagnostic fixtures remain in the full archive. They
are not deleted, rewritten or promoted to successful results. This package's
curated copies do not reproduce every file in that archive.

The archived per-run README and M4 trace text also retain their original command
strings and paths; use the new repository README for navigation and setup. The
canonical replay directories include their original inert Dockerfile/override
artifacts as hashed evidence, not new application implementation.

`docs/submission/.gitattributes` disables text conversion for `evidence/**` so
Git retains the exact source bytes. Whitespace checks are disabled only for
archival evidence, whose original CRLF/CSV line endings must remain intact.
Source and destination hashes were checked
during packaging. Both canonical `COMPLETE.json` → manifest hashes and every
manifest-listed artifact hash are checked independently as packaging validation.

No `.env`, credentials file, SQLite database, WAL or SHM file is committed.
Published artifacts retain sanitized evidence and synthetic account data. The
original files and accepted source remain untouched. A preserved source hash is
an integrity check, not an external signature authenticating the reviewer.
