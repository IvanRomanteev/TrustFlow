export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export interface PlanStep {
  step_id: string;
  tool_name: string;
  intent_id: string;
  operation_id: string;
  args: Record<string, unknown>;
  risk: string;
  rationale: string;
}

export interface WorkflowRun {
  run_id: string;
  goal: { title: string; description: string; requested_by: string; require_evidence: boolean };
  status: string;
  plan: { version: number; plan_hash: string; steps: PlanStep[] } | null;
  step_results: { step_id: string; tool_name: string; status: string; data: Record<string, unknown>; error: string | null; idempotency_key: string }[];
  verification: {
    passed: boolean;
    checks: { name: string; passed: boolean; detail: string }[];
    issues: string[];
    semantic: { verdict: string; confidence: number; issues: string[]; summary: string; model: string } | null;
  } | null;
  approvals: { plan_version: number; plan_hash: string; decision: string; actor: string; reason: string; created_at: string }[];
  intents: { intent_id: string; tool_name: string; status: string; authorized: boolean; authorized_payload: Record<string, unknown>; proposed_payload: Record<string, unknown> }[];
  approved_hash: string;
  evidence_spans: Record<string, unknown>[];
  ledger_snapshot: { snapshot_id?: string; snapshot_hash?: string } | null;
  llm_calls: Record<string, unknown>[];
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface AuditEvent {
  run_id: string;
  ts: string;
  actor: string;
  action: string;
  payload_hash: string;
  detail: Record<string, unknown>;
}

async function request<T>(path: string, init?: RequestInit, token?: string): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["X-Approval-Token"] = token;
  const res = await fetch(`${API_URL}${path}`, { ...init, headers: { ...headers, ...(init?.headers ?? {}) } });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch { /* keep status text */ }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => request<{ persistence: string; llm_mode: string }>("/health"),
  listRuns: () => request<WorkflowRun[]>("/workflows"),
  getRun: (id: string) => request<WorkflowRun>(`/workflows/${id}`),
  getAudit: (id: string) => request<{ events: AuditEvent[] }>(`/workflows/${id}/audit`),
  create: (title: string, description: string, requireEvidence: boolean) =>
    request<WorkflowRun>("/workflows", {
      method: "POST",
      body: JSON.stringify({ title, description, require_evidence: requireEvidence }),
    }),
  approve: (id: string, actor: string, planHash: string, token: string) =>
    request<WorkflowRun>(`/workflows/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ actor, plan_hash: planHash }),
    }, token),
  reject: (id: string, actor: string, planHash: string, token: string) =>
    request<WorkflowRun>(`/workflows/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ actor, plan_hash: planHash, reason: "Rejected in demo UI" }),
    }, token),
  verifyRetry: (id: string, token: string) =>
    request<WorkflowRun>(`/workflows/${id}/verify-retry`, { method: "POST", body: "{}" }, token),
  reconcile: (id: string, token: string) =>
    request<WorkflowRun>(`/workflows/${id}/reconcile`, { method: "POST", body: "{}" }, token),
};
