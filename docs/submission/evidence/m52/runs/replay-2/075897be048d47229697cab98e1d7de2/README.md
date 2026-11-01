# M5.2 replay evidence

Synthetic local demo; FakeLLMProvider only.

Invocation: `075897be048d47229697cab98e1d7de2`; exact SHA: `38cc3942dbd715681d59bfd893e47643210294a9`; dirty: `False`.

Replay: `python scripts/run_m52_demo.py --out artifacts/m5.2/replay`

Use `--compare <this-directory>` for normalized comparison. See docs/m52-demo.md for fault injection, seed, scenario narratives, screenshot map and limits.

commands.jsonl records actual commands, cwd and exit codes. Browser logs exclude headers/bodies; no HAR/trace/video retains approval tokens. ledger-*.json are independent read-only SQLite transactions including WAL. S4 uses a real timed-out held worker; S6 uses actual SIGKILL after observed COMMIT, followed by a separately labelled graceful restart. Cleanup and original-resource preservation are mandatory.
