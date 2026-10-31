# TrustFlow

Self-verifying multi-agent workflows for the **Meta Global AI Developer Hackathon 2026**.

TrustFlow runs a `Planner -> Human Approval -> Executor -> Verifier` loop over
**synthetic financial data only**. No real money, no real banking APIs, no secrets
in code or logs ONLY FOR HACKATHON USING :D

## Quickstart (Windows PowerShell)

```powershell
python -m venv --without-pip .venv
pip --python .venv\Scripts\python.exe install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn trustflow.api.app:app --app-dir src --port 8000
```

On macOS/Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn trustflow.api.app:app --app-dir src --port 8000
```

Open http://127.0.0.1:8000/docs for the interactive API.

## Demo flow

1. `POST /workflows` with `{"title": "Transfer 5000", "description": "transfer 5000 to bob"}`
   returns `AWAITING_APPROVAL` plus a workflow ID and `plan.plan_hash`.
2. `POST /workflows/{id}/approve` with `{"actor": "mgr", "plan_hash": "<hash>"}` resumes
   execution; the Verifier then confirms or triggers a bounded re-plan.
3. `POST /workflows/{id}/reject` terminates the run safely with no transfer.
4. `GET /workflows/{id}/audit` shows the full hashed audit trail.

Low-risk goals (e.g. a balance check) execute and verify immediately.

## M3 demo (durable state + UI)

One command (Docker daemon required):

```powershell
docker compose up --build
```

Then open http://127.0.0.1:3000 in a browser. Approval token for
panel 3: `demo-approval-token`. Without Docker, run the backend with
`TRUSTFLOW_DB_PATH=data/trustflow.db` and
`TRUSTFLOW_APPROVAL_TOKEN=demo-approval-token` set, plus
`npm run start` in `frontend/` after `npm install` and
`npm run build`. Workflow, ledger, approval and audit state lives in
SQLite and survives restarts; see [docs/m3.md](docs/m3.md).

## Safety

- Risk is decided by the deterministic backend `PolicyEngine`, never by the LLM alone.
- Approvals are bound to an immutable plan version + SHA-256 hash; repeats are rejected.
- Mutating tools are idempotent; succeeded steps are never re-executed on retry/resume.
- Retries, steps, and step timeouts are bounded in `Settings`.
- API keys come only from `TRUSTFLOW_LLM_API_KEY`; logs redact secret-looking values.

## M4 offline evaluation

One command runs the maintained 33-case offline evaluation (fake
provider, no network, no paid calls) and writes JSON/CSV/Markdown
reports, per-run traces and a reproducibility manifest:

```powershell
.\.venv\Scripts\python.exe -m trustflow.evaluation --out artifacts/m4-eval --repeat 2 --ablations
```

Exit codes, in precedence order (all findings are preserved in the
published `repeats.json`/`COMPLETE.json` regardless of which code
wins): 2 = critical safety violation in any repeat, 3 = harness or
infrastructure failure, 4 = verdicts not reproducible across repeats,
1 = case assertion failures, 0 = valid successful gate. Invalid
command-line arguments exit 64 (usage error, distinct from evaluation
outcomes). Artifacts are staged and published atomically; a failed
run never overwrites a previously published successful report, and
`COMPLETE.json` exists only for a fully published run. If a later
repeat or stage fails, completed-repeat findings are retained in a
separate `<name>.failed-<attempt>/FAILED_ATTEMPT.json` and in the
sanitized stdout summary; no completion marker is created. These are
offline workflow safety regression results, not live-model
performance. See [docs/m4.md](docs/m4.md).

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src
```

See [docs/architecture.md](docs/architecture.md) for the design.
