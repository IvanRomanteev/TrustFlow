"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type AuditEvent, type WorkflowRun } from "@/lib/api";

const RUN_KEY = "trustflow-run-id";
const TOKEN_KEY = "trustflow-approval-token";

function StatusLine({ run }: { run: WorkflowRun }) {
  return (
    <span className={`status status-${run.status}`}>{run.status}</span>
  );
}

export default function Home() {
  const [title, setTitle] = useState("Transfer 5000");
  const [description, setDescription] = useState("transfer 5000 to bob");
  const [requireEvidence, setRequireEvidence] = useState(false);
  const [actor, setActor] = useState("demo-manager");
  const [token, setToken] = useState("");
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [run, setRun] = useState<WorkflowRun | null>(null);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [backend, setBackend] = useState<string>("checking…");
  const runIdRef = useRef<string | null>(null);
  // F6: monotonic refresh generation. A response is applied only if
  // no newer refresh/action/selection started meanwhile — an already
  // completed stale response can never overwrite newer state, even
  // when its fetch finished before a mutation but resolves after it.
  const generationRef = useRef(0);

  const selectRun = useCallback((id: string | null) => {
    generationRef.current += 1; // invalidate in-flight refreshes
    runIdRef.current = id;
    if (id) localStorage.setItem(RUN_KEY, id);
    else localStorage.removeItem(RUN_KEY);
  }, []);

  const refresh = useCallback(async () => {
    const generation = ++generationRef.current;
    const stale = () => generation !== generationRef.current;
    try {
      const [list, health] = await Promise.all([api.listRuns(), api.health()]);
      if (stale()) return;
      setRuns(list);
      setBackend(`backend ok · persistence=${health.persistence} · llm=${health.llm_mode} (fake = deterministic, no Meta call)`);
      const id = runIdRef.current ?? localStorage.getItem(RUN_KEY);
      const target = id && list.some((r) => r.run_id === id) ? id : list[0]?.run_id;
      if (target) {
        if (target !== runIdRef.current) {
          runIdRef.current = target;
          localStorage.setItem(RUN_KEY, target);
        }
        const [fresh, trail] = await Promise.all([api.getRun(target), api.getAudit(target)]);
        // Ignore responses for a run that is no longer selected, and
        // any response superseded by a newer generation (F6).
        if (stale() || runIdRef.current !== target) return;
        setRun(fresh);
        setAudit(trail.events);
      } else {
        setRun(null);
        setAudit([]);
      }
    } catch (err) {
      if (stale()) return;
      setBackend(`backend unreachable: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, [selectRun]);

  useEffect(() => {
    setToken(sessionStorage.getItem(TOKEN_KEY) ?? "");
    void refresh();
    const timer = setInterval(() => void refresh(), 2000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function act(label: string, fn: () => Promise<WorkflowRun>,
                      followResult = false) {
    // N2: capture the selected run before awaiting. A late action
    // response must never steal a newer user selection: it may only
    // update the visible run when that run is still selected (or the
    // action explicitly creates/selects its result, e.g. Create).
    const startedFor = runIdRef.current;
    try {
      const updated = await fn();
      // The action response is the newest authoritative state for
      // its own run: it supersedes refreshes started before it (F6).
      generationRef.current += 1;
      if (!followResult && runIdRef.current !== startedFor) {
        setMessage(
          `${label}: finished for a previously selected run `
          + `(backend status ${updated.status}); your current selection is unchanged.`);
        await refresh(); // refreshes list + currently selected run only
        return;
      }
      selectRun(updated.run_id);
      setRun(updated);
      setMessage(`${label}: backend status is now ${updated.status}.`);
      await refresh();
    } catch (err) {
      // Errors are reported as a notice only; they never overwrite
      // the selected run, its evidence, or its audit (N2-F).
      setMessage(`${label} failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  const planHash = run?.plan?.plan_hash ?? "";
  const canDecide = run?.status === "AWAITING_APPROVAL" && Boolean(planHash);

  return (
    <>
      <div className="sandbox-banner">
        SANDBOX / NO REAL FUNDS
        <small>Synthetic ledger only (acc_alice / acc_bob / acc_ops). Fake LLM mode is deterministic and makes no Meta API call.</small>
      </div>
      <main>
        <header className="page-head">
          <h1>TrustFlow — self-verifying agent workflows</h1>
          <p>
            Planner proposes, deterministic policy decides risk, a human approves the exact
            plan hash, the executor runs idempotent synthetic tools, and an independent
            verifier checks ledger evidence. Backend state survives restarts.
          </p>
          <p className="muted">{backend} · polling every 2s · FastAPI is the source of truth</p>
          {message && <div className="notice">{message}</div>}
        </header>
        <div className="grid">
          <section className="panel" aria-label="Task input">
            <h2>1 · Task input</h2>
            <p className="sub">Create a synthetic workflow. Try “Check balance”, “Transfer 100” (executes), or “Transfer 5000” (HIGH risk, needs approval).</p>
            <label htmlFor="title">Title</label>
            <input id="title" value={title} onChange={(e) => setTitle(e.target.value)} />
            <label htmlFor="desc">Description</label>
            <textarea id="desc" value={description} onChange={(e) => setDescription(e.target.value)} />
            <label>
              <input type="checkbox" checked={requireEvidence}
                onChange={(e) => setRequireEvidence(e.target.checked)}
                style={{ width: "auto", marginRight: 8 }} />
              Require mandatory evidence (M2 opt-in)
            </label>
            <button className="primary" onClick={() => void act("Create workflow",
              async () => {
                const created = await api.create(title, description, requireEvidence);
                return created;
              }, true)}>
              Create workflow
            </button>
            <label htmlFor="runs">Runs (durable, newest first)</label>
            <select id="runs" value={run?.run_id ?? ""}
              onChange={(e) => { selectRun(e.target.value || null); void refresh(); }}>
              <option value="">— none yet —</option>
              {runs.map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {r.goal.title} · {r.status}
                </option>
              ))}
            </select>
            <p className="muted">Selected run id is kept in this browser, so a refresh restores the same workflow from the backend.</p>
          </section>

          <section className="panel" aria-label="Run timeline">
            <h2>2 · Run timeline</h2>
            <p className="sub">Plan, step results, verification evidence and audit — exactly as the backend reports them.</p>
            {!run && <p className="muted">No workflow selected yet. Create one in panel 1.</p>}
            {run && (
              <>
                <p>Goal: <strong>{run.goal.title}</strong> · Status: <StatusLine run={run} /></p>
                <p className="mono">run_id: {run.run_id}</p>
                {run.error && <div className="notice bad"><strong>Error / blocked:</strong> {run.error}</div>}
                {run.status === "VERIFICATION_PENDING" && (
                  <div className="notice warn">
                    <strong>Pending verification — not completed.</strong> Any committed
                    transfer stays committed and will not be re-executed.
                    <br />
                    <button onClick={() => void act("Verify-retry", () => api.verifyRetry(run.run_id, token))}>
                      Retry verification only
                    </button>
                  </div>
                )}
                {run.status === "EXECUTION_UNCERTAIN" && (
                  <div className="notice warn">
                    <strong>Execution outcome uncertain.</strong> No replacement transfer
                    is started; reconcile reads workers/ledger only.
                    <br />
                    <button onClick={() => void act("Reconcile", () => api.reconcile(run.run_id, token))}>
                      Reconcile from ledger
                    </button>
                  </div>
                )}
                <h3>Plan steps</h3>
                <table>
                  <thead><tr><th>Step</th><th>Tool</th><th>Risk</th><th>Result</th></tr></thead>
                  <tbody>
                    {(run.plan?.steps ?? []).map((s) => {
                      const res = run.step_results.find((r) => r.step_id === s.step_id);
                      return (
                        <tr key={s.step_id}>
                          <td className="mono">{s.step_id}</td>
                          <td>{s.tool_name}</td>
                          <td>{s.risk}</td>
                          <td>{res ? `${res.status}${res.data?.tx_id ? ` · ${String(res.data.tx_id)}` : ""}` : "— pending —"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                <h3>Verification evidence</h3>
                {!run.verification && <p className="muted">No verification report yet.</p>}
                {run.verification && (
                  <>
                    {/* F7: deterministic verdict comes from the actual
                        deterministic checks only; verification.passed
                        is the overall result (semantic included). */}
                    <p>Deterministic: {run.verification.checks.length === 0
                      ? <span>— unknown (no checks reported) —</span>
                      : <span className={run.verification.checks.every((c) => c.passed) ? "check-pass" : "check-fail"}>
                          {run.verification.checks.every((c) => c.passed) ? "PASS" : "FAIL"}</span>}
                      {run.verification.semantic && <> · Semantic ({run.verification.semantic.model}): {run.verification.semantic.verdict}</>}
                      {" "}· Overall: <span className={run.verification.passed ? "check-pass" : "check-fail"}>
                        {run.verification.passed ? "PASS" : "FAIL"}</span>
                    </p>
                    <table>
                      <thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr></thead>
                      <tbody>
                        {run.verification.checks.map((c) => (
                          <tr key={c.name}>
                            <td className="mono">{c.name}</td>
                            <td className={c.passed ? "check-pass" : "check-fail"}>{c.passed ? "pass" : "FAIL"}</td>
                            <td>{c.detail}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {run.verification.issues.length > 0 && (
                      <div className="notice bad">{run.verification.issues.join("; ")}</div>
                    )}
                  </>
                )}
                <p className="muted">
                  Evidence spans: {run.evidence_spans.length}
                  {run.ledger_snapshot?.snapshot_id && <> · snapshot <span className="mono">{run.ledger_snapshot.snapshot_id}</span></>}
                  {" "}· LLM calls recorded: {run.llm_calls.length}
                </p>
                <h3>Audit trail (append-only, hashed)</h3>
                <ul className="audit">
                  {audit.map((e, i) => (
                    <li key={`${e.ts}-${i}`}>
                      <span className="mono">{e.ts}</span> · {e.actor} · <strong>{e.action}</strong>
                    </li>
                  ))}
                  {audit.length === 0 && <li className="muted">No audit events yet.</li>}
                </ul>
              </>
            )}
          </section>

          <section className="panel" aria-label="Action approval and evidence">
            <h2>3 · Approval &amp; evidence</h2>
            <p className="sub">Approval is bound to this exact plan hash. A different plan, a replay, or a missing token cannot execute it.</p>
            <label htmlFor="actor">Approver</label>
            <input id="actor" value={actor} onChange={(e) => setActor(e.target.value)} />
            <label htmlFor="token">Approval token (demo secret, session only)</label>
            <input id="token" type="password" value={token}
              onChange={(e) => { setToken(e.target.value); sessionStorage.setItem(TOKEN_KEY, e.target.value); }}
              placeholder="Required for approve / reject / recovery" />
            {!run && <p className="muted">Select or create a workflow first.</p>}
            {run && (
              <>
                <p>Plan version: {run.plan?.version ?? "—"} · Status: <StatusLine run={run} /></p>
                <p>Exact approved plan hash:<br /><span className="mono">{planHash || "— no plan yet —"}</span></p>
                {run.approved_hash && <p>Approved hash on record:<br /><span className="mono">{run.approved_hash}</span></p>}
                <h3>Operations in this plan</h3>
                <table>
                  <thead><tr><th>Operation</th><th>Detail</th><th>Intent</th></tr></thead>
                  <tbody>
                    {(run.plan?.steps ?? []).map((s) => {
                      const intent = run.intents.find((i) => i.intent_id === (s.intent_id || s.operation_id));
                      const a = s.args;
                      const detail = s.tool_name === "simulate_transfer"
                        ? `${String(a.from_account ?? "")} → ${String(a.to_account ?? "")} · ${String(a.amount ?? "")} ${String(a.currency ?? "USD")}`
                        : JSON.stringify(a);
                      return (
                        <tr key={s.step_id}>
                          <td className="mono">{s.operation_id || s.step_id}</td>
                          <td>{s.tool_name}<br /><span className="muted">{detail}</span></td>
                          <td>{intent ? `${intent.status}${intent.authorized ? " · authorized" : ""}` : "—"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                {run.approvals.length > 0 && (
                  <>
                    <h3>Decisions on record</h3>
                    <ul className="audit">
                      {run.approvals.map((a, i) => (
                        <li key={i}>{a.decision} by {a.actor} · v{a.plan_version} · <span className="mono">{a.plan_hash.slice(0, 16)}…</span></li>
                      ))}
                    </ul>
                  </>
                )}
                <div className="row">
                  <button className="primary" disabled={!canDecide || !token}
                    onClick={() => void act("Approve", () => api.approve(run.run_id, actor, planHash, token))}>
                    Approve exact plan
                  </button>
                  <button className="danger" disabled={!canDecide || !token}
                    onClick={() => void act("Reject", () => api.reject(run.run_id, actor, planHash, token))}>
                    Reject
                  </button>
                </div>
                {!canDecide && <p className="muted">Approve/Reject activate only while the backend reports AWAITING_APPROVAL.</p>}
                {canDecide && !token && <p className="muted">Enter the approval token to enable the decision buttons (backend returns 401/403 without a valid one).</p>}
              </>
            )}
          </section>
        </div>
      </main>
    </>
  );
}
