# TrustFlow

Self-verifying multi-agent workflows for the **Meta Global AI Developer Hackathon 2026**
(Build for Your World, Indie Developer track).

TrustFlow runs a `Planner -> Human Approval -> Executor -> Verifier` loop over
**synthetic financial data only**. No real money, no real banking APIs, no secrets
in code or logs.

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

## Safety

- Risk is decided by the deterministic backend `PolicyEngine`, never by the LLM alone.
- Approvals are bound to an immutable plan version + SHA-256 hash; repeats are rejected.
- Mutating tools are idempotent; succeeded steps are never re-executed on retry/resume.
- Retries, steps, and step timeouts are bounded in `Settings`.
- API keys come only from `TRUSTFLOW_LLM_API_KEY`; logs redact secret-looking values.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src
```

See [docs/architecture.md](docs/architecture.md) for the design.
