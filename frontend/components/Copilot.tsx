"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Alert, Button, Card, Field, Select, TextInput } from "@/components/ui";
import {
  type AiStatus,
  type Comparison,
  type Diagnosis,
  type DiagnosisAction,
  type DiagnosisDetail,
  type IacKind,
  type IacResult,
  type OpenProblem,
} from "@/lib/ai";
import { useLoad } from "@/lib/ansible";
import { api, message } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { InventoryNode } from "@/lib/inventory";

// Same rule as the server (janus_ops/agents/exec.py): no shell syntax can run on a node
const SHELL_CHARS = /[;|&`$<>()\\\n\r]/;

function hasShellSyntax(command: string): boolean {
  return SHELL_CHARS.test(command);
}

const RISK_STYLE: Record<string, string> = {
  low: "text-ok",
  medium: "text-warn",
  high: "text-crit",
};

export function CopilotConsole() {
  const { can } = useAuth();
  const status = useLoad<AiStatus>("/ai/status");
  const problems = useLoad<OpenProblem[]>("/problems?status=open&limit=100");
  // the palette (Ctrl+K) links here with ?problem=<id>: that problem is already selected; nothing is sent until Diagnose
  const wanted = useSearchParams().get("problem");
  const [problemId, setProblemId] = useState<string | null>(wanted);
  const [diagnosisId, setDiagnosisId] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const diagnoses = useLoad<Diagnosis[]>(problemId ? `/ai/diagnoses?problem_id=${problemId}` : null);
  const detail = useLoad<DiagnosisDetail>(diagnosisId ? `/ai/diagnoses/${diagnosisId}` : null);
  const pending = (diagnoses.data ?? []).some((d) => d.status === "pending" || d.second_status === "pending");

  // While a diagnosis runs, the list is refreshed until it finishes
  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => {
      diagnoses.reload();
      detail.reload();
    }, 2500);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending]);

  const selectedProblem = (problems.data ?? []).find((p) => p.id === problemId) ?? null;
  const canUse = can("ai.use");
  const canApprove = can("ai.approve");
  const canExecute = can("actions.execute");

  async function diagnose() {
    if (!problemId) return;
    setBusy(true);
    const r = await api<{ id: string }>(`/ai/problems/${problemId}/diagnose`, { method: "POST" });
    setBusy(false);
    if (!r.ok) {
      setNotice({ kind: "error", text: message(r.code) });
      return;
    }
    setNotice({ kind: "ok", text: "Diagnosis requested. The data is sent to the AI endpoint; the result appears here." });
    setDiagnosisId(r.data.id);
    diagnoses.reload();
  }

  async function decide(action: DiagnosisAction, change: Record<string, unknown>) {
    if (!diagnosisId) return;
    const r = await api<DiagnosisDetail>(`/ai/diagnoses/${diagnosisId}/actions/${action.id}`, {
      method: "PATCH",
      body: change,
    });
    if (!r.ok) {
      setNotice({ kind: "error", text: message(r.code) });
      return;
    }
    setNotice({ kind: "ok", text: `Action "${action.title}" updated.` });
    detail.reload();
    diagnoses.reload();
  }

  async function secondOpinion() {
    if (!diagnosisId) return;
    setBusy(true);
    const r = await api(`/ai/diagnoses/${diagnosisId}/second-opinion`, { method: "POST" });
    setBusy(false);
    if (!r.ok) {
      setNotice({ kind: "error", text: message(r.code) });
      return;
    }
    setNotice({ kind: "ok", text: "Second opinion requested: the same data goes to the reviewing model, then the two answers are compared." });
    detail.reload();
    diagnoses.reload();
  }

  async function execute(action: DiagnosisAction) {
    if (!diagnosisId) return;
    setBusy(true);
    const r = await api<DiagnosisDetail>(`/ai/diagnoses/${diagnosisId}/actions/${action.id}/execute`, {
      method: "POST",
    });
    setBusy(false);
    if (!r.ok) {
      setNotice({ kind: "error", text: `Not run: ${message(r.code)}` });
    } else {
      const code = r.data.actions.find((a) => a.id === action.id)?.result?.exit_code;
      setNotice({ kind: "ok", text: `"${action.title}" ran on ${r.data.node_name} (exit ${code ?? "n/a"}).` });
    }
    detail.reload();
    diagnoses.reload();
  }

  return (
    <div className="space-y-4">
      {status.data && !status.data.enabled && (
        <Alert kind="info">
          The AI Copilot is off: set JANUS_AI_BASE_URL and JANUS_AI_MODEL to an OpenAI-compatible https endpoint.
        </Alert>
      )}
      {notice && <Alert kind={notice.kind}>{notice.text}</Alert>}
      {problems.error && <Alert>{problems.error}</Alert>}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <Card title="Open problems">
          <div className="space-y-2">
            {(problems.data ?? []).length === 0 && <p className="text-sm text-fg-muted">No open problems.</p>}
            {(problems.data ?? []).map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => {
                  setProblemId(p.id);
                  setDiagnosisId(null);
                }}
                className={`w-full rounded border px-3 py-2 text-left text-sm ${
                  p.id === problemId ? "border-ai bg-elevated" : "border-line"
                }`}
              >
                <span className="font-medium">{p.node_name}</span> · {p.check_name}
                <span className="ml-2 text-xs uppercase text-fg-muted">{p.state}</span>
              </button>
            ))}
          </div>
        </Card>

        <Card
          title={selectedProblem ? `${selectedProblem.node_name} · ${selectedProblem.check_name}` : "Diagnosis"}
          actions={
            canUse && selectedProblem ? (
              <Button variant="primary" disabled={busy || !status.data?.enabled} onClick={() => void diagnose()}>
                Diagnose
              </Button>
            ) : undefined
          }
        >
          {!selectedProblem && <p className="text-sm text-fg-muted">Choose a problem to see its diagnoses.</p>}
          {selectedProblem && (
            <div className="space-y-3">
              {selectedProblem.last_output && (
                <p className="font-mono text-xs text-fg-muted">Last output: {selectedProblem.last_output}</p>
              )}
              <Field label="Past diagnoses">
                <Select value={diagnosisId ?? ""} onChange={(e) => setDiagnosisId(e.target.value || null)}>
                  <option value="">Choose one</option>
                  {(diagnoses.data ?? []).map((d) => (
                    <option key={d.id} value={d.id}>
                      {new Date(d.requested_at).toLocaleString()} · {d.status} · {d.requested_by}
                    </option>
                  ))}
                </Select>
              </Field>
              {detail.data && (
                <DiagnosisView
                  detail={detail.data}
                  canApprove={canApprove}
                  canExecute={canExecute}
                  busy={busy}
                  onDecide={decide}
                  onExecute={execute}
                  reviewModel={status.data?.review_enabled ? status.data.review_model : null}
                  onSecondOpinion={secondOpinion}
                />
              )}
            </div>
          )}
        </Card>
      </div>
      {canUse && <IacCard enabled={!!status.data?.enabled} />}
    </div>
  );
}

function DiagnosisView({
  detail,
  canApprove,
  canExecute,
  busy,
  onDecide,
  onExecute,
  reviewModel,
  onSecondOpinion,
}: {
  detail: DiagnosisDetail;
  canApprove: boolean;
  canExecute: boolean;
  busy: boolean;
  onDecide: (action: DiagnosisAction, change: Record<string, unknown>) => Promise<void>;
  onExecute: (action: DiagnosisAction) => Promise<void>;
  reviewModel: string | null;
  onSecondOpinion: () => Promise<void>;
}) {
  return (
    <div className="space-y-3 text-sm">
      <p className="text-xs text-fg-muted">
        {detail.model} · requested by {detail.requested_by} · {detail.status}
        {detail.error ? ` · ${detail.error}` : ""}
      </p>
      {detail.summary && <p className="whitespace-pre-wrap">{detail.summary}</p>}
      {detail.status === "pending" && <p className="text-fg-muted">Waiting for the model…</p>}
      {detail.actions.length > 0 && (
        <ol className="space-y-2">
          {detail.actions.map((action) => (
            <ActionRow
              key={action.id}
              action={action}
              canApprove={canApprove}
              canExecute={canExecute}
              busy={busy}
              onDecide={onDecide}
              onExecute={onExecute}
            />
          ))}
        </ol>
      )}
      {detail.status === "done" && (
        <SecondOpinion detail={detail} reviewModel={reviewModel} busy={busy} onAsk={onSecondOpinion} />
      )}
      {(detail.steps ?? []).length > 0 && (
        <details className="text-xs text-fg-muted" open={(detail.steps ?? []).some((s) => s.mode === "write")}>
          <summary className="cursor-pointer">
            What the AI looked at and did ({(detail.steps ?? []).length} tool calls
            {(detail.steps ?? []).some((s) => s.mode === "write") ? ", including changes" : ""})
          </summary>
          <ol className="mt-2 space-y-2">
            {(detail.steps ?? []).map((s) => (
              <li key={s.seq} className="rounded bg-elevated p-2">
                <p className="font-mono">
                  {s.mode === "write" && <span className="mr-1 rounded bg-warn/20 px-1 text-warn">changed</span>}
                  {s.tool} {JSON.stringify(s.arguments)}
                  <span className="ml-2 text-fg-muted">{s.duration_ms ?? 0} ms</span>
                </p>
                {s.error && <p className="text-crit">{s.error}</p>}
                {s.result && (
                  <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap">{s.result}</pre>
                )}
              </li>
            ))}
          </ol>
        </details>
      )}
      <details className="text-xs text-fg-muted">
        <summary className="cursor-pointer">What was sent to the model</summary>
        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-elevated p-2">{detail.prompt}</pre>
      </details>
    </div>
  );
}

function ActionRow({
  action,
  canApprove,
  canExecute,
  busy,
  onDecide,
  onExecute,
}: {
  action: DiagnosisAction;
  canApprove: boolean;
  canExecute: boolean;
  busy: boolean;
  onDecide: (action: DiagnosisAction, change: Record<string, unknown>) => Promise<void>;
  onExecute: (action: DiagnosisAction) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(action.title);
  const [description, setDescription] = useState(action.description);
  const [command, setCommand] = useState(action.command ?? "");
  const proposed = action.status === "proposed";

  return (
    <li className="rounded border border-line p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{action.title}</span>
        <span className={`text-xs uppercase ${RISK_STYLE[action.risk] ?? ""}`}>{action.risk}</span>
        <span className="text-xs uppercase text-fg-muted">{action.status}</span>
        {action.edited && <span className="text-xs text-fg-muted">edited</span>}
      </div>
      {!editing && <p className="mt-1 whitespace-pre-wrap text-fg-muted">{action.description}</p>}
      {!editing && action.command && <code className="mt-1 block font-mono text-xs">{action.command}</code>}
      {action.decided_by && (
        <p className="mt-1 text-xs text-fg-muted">
          {action.status} by {action.decided_by}
        </p>
      )}
      {action.result && (
        <div className="mt-2 space-y-1">
          <p className="text-xs text-fg-muted">
            exit {action.result.exit_code ?? "–"} · {action.result.reason || "no reason"} · by {action.result.by}
            {action.result.truncated ? " · output truncated" : ""}
          </p>
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded bg-elevated p-2 font-mono text-xs">
            {action.result.output || "(no output)"}
          </pre>
        </div>
      )}
      {canExecute && action.status === "approved" && !editing && action.command && (
        <div className="mt-2 space-y-1">
          {hasShellSyntax(action.command) ? (
            <p className="text-xs text-warn">
              This command uses shell syntax and cannot run on a node. Edit it to a single program with arguments.
            </p>
          ) : (
            <Button variant="primary" disabled={busy} onClick={() => void onExecute(action)}>
              Run on node
            </Button>
          )}
        </div>
      )}
      {editing && (
        <div className="mt-2 space-y-2">
          <Field label="Title">
            <TextInput value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} />
          </Field>
          <Field label="Description">
            <TextInput value={description} onChange={(e) => setDescription(e.target.value)} maxLength={2000} />
          </Field>
          <Field label="Command (for display only)">
            <TextInput value={command} onChange={(e) => setCommand(e.target.value)} maxLength={500} />
          </Field>
          <div className="flex gap-2">
            <Button
              onClick={() => {
                void onDecide(action, { title, description, command });
                setEditing(false);
              }}
            >
              Save changes
            </Button>
            <Button onClick={() => setEditing(false)}>Cancel</Button>
          </div>
        </div>
      )}
      {canApprove && proposed && !editing && (
        <div className="mt-2 flex flex-wrap gap-2">
          <Button variant="primary" onClick={() => void onDecide(action, { status: "approved" })}>
            Approve
          </Button>
          <Button onClick={() => void onDecide(action, { status: "rejected" })}>Reject</Button>
          <Button onClick={() => setEditing(true)}>Edit</Button>
        </div>
      )}
    </li>
  );
}

const AGREEMENT: Record<string, { label: string; className: string }> = {
  agree: { label: "The two models agree", className: "text-ok" },
  partial: { label: "Partly different", className: "text-warn" },
  disagree: { label: "The two models disagree", className: "text-crit" },
};
const PREFERRED: Record<string, string> = {
  first: "The first diagnosis is better supported",
  second: "The second opinion is better supported",
  both: "Both are supported",
  neither: "Neither is reliable on this evidence",
};

function List({ title, items }: { title: string; items?: string[] }) {
  if (!items?.length) return null;
  return (
    <div>
      <p className="text-xs uppercase text-fg-muted">{title}</p>
      <ul className="list-disc space-y-0.5 pl-5">
        {items.map((i) => (
          <li key={i}>{i}</li>
        ))}
      </ul>
    </div>
  );
}

function ComparisonView({ c }: { c: Comparison }) {
  if (c.error) return <p className="text-xs text-warn">The comparison failed ({message(c.error)}); the two answers are shown as they are.</p>;
  const agreement = AGREEMENT[c.agreement ?? ""];
  return (
    <div className="space-y-2 rounded border border-line bg-elevated p-3">
      <p className={`font-medium ${agreement?.className ?? ""}`}>{agreement?.label ?? "Comparison"}</p>
      {c.preferred && <p className="text-xs text-fg-secondary">{PREFERRED[c.preferred]}</p>}
      <List title="Common ground" items={c.common} />
      <List title="Differences" items={c.differences} />
      {c.verdict && <p className="whitespace-pre-wrap">{c.verdict}</p>}
      <p className="text-[11px] text-fg-muted">
        Compared by {c.judge ?? "the main model"}. A model judging models is a hint, not proof: check the evidence.
      </p>
    </div>
  );
}

function SecondOpinion({
  detail,
  reviewModel,
  busy,
  onAsk,
}: {
  detail: DiagnosisDetail;
  reviewModel: string | null;
  busy: boolean;
  onAsk: () => Promise<void>;
}) {
  const running = detail.second_status === "pending";
  return (
    <div className="space-y-2 border-t border-line pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">Second opinion</span>
        <Button disabled={busy || running || !reviewModel} onClick={() => void onAsk()}>
          {running ? "Asking…" : detail.second_status ? "Ask again" : "Ask for a second opinion"}
        </Button>
        {!reviewModel && <span className="text-xs text-fg-muted">Set JANUS_AI_REVIEW_MODEL to enable it.</span>}
        {reviewModel && <span className="text-xs text-fg-muted">reviewing model: {reviewModel}</span>}
      </div>
      {running && <p className="text-fg-muted">Waiting for the reviewing model, then the comparison…</p>}
      {detail.second_status === "error" && <Alert>{message(detail.second_error ?? "run_error")}</Alert>}
      {detail.second_status === "done" && (
        <div className="space-y-3">
          {detail.comparison && <ComparisonView c={detail.comparison} />}
          <div>
            <p className="text-xs text-fg-muted">{detail.second_model} · second diagnosis (proposals are informative only)</p>
            <p className="whitespace-pre-wrap">{detail.second_summary}</p>
            {detail.second_actions.length > 0 && (
              <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-fg-secondary">
                {detail.second_actions.map((a) => (
                  <li key={a.id}>
                    <span className={RISK_STYLE[a.risk] ?? ""}>{a.risk}</span> · {a.title}
                    {a.command ? <code className="ml-1 font-mono">{a.command}</code> : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

const IAC_KINDS: { value: IacKind; label: string }[] = [
  { value: "ansible", label: "Ansible playbook" },
  { value: "compose", label: "Docker Compose file" },
  { value: "shell", label: "Shell script" },
];

function IacCard({ enabled }: { enabled: boolean }) {
  const nodes = useLoad<InventoryNode[]>("/nodes");
  const [nodeId, setNodeId] = useState("");
  const [kind, setKind] = useState<IacKind>("ansible");
  const [request, setRequest] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resultId, setResultId] = useState<string | null>(null);
  const result = useLoad<IacResult>(resultId ? `/ai/iac/${resultId}` : null);
  const running = result.data?.status === "pending";

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => result.reload(), 2500);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running]);

  async function generate(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const r = await api<{ id: string }>("/ai/iac", { method: "POST", body: { node_id: nodeId, kind, request } });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    setResultId(r.data.id);
  }

  const data = result.data;
  return (
    <Card title="Generate code for a node (IaC)">
      <form onSubmit={generate} className="space-y-3">
        <p className="text-xs text-fg-secondary">
          The model sees the node&apos;s inventory (no secrets), last-hour metrics and checks. The result is text to read and
          edit: nothing is run or deployed from here.
        </p>
        {error && <Alert>{error}</Alert>}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Node">
            <Select value={nodeId} onChange={(e) => setNodeId(e.target.value)} required>
              <option value="">Choose a node</option>
              {(nodes.data ?? []).map((n) => (
                <option key={n.id} value={n.id}>
                  {n.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="What to write">
            <Select value={kind} onChange={(e) => setKind(e.target.value as IacKind)}>
              {IAC_KINDS.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <Field label="Request" hint="For example: rotate and compress the nginx logs weekly, keep 8 weeks">
          <TextInput value={request} onChange={(e) => setRequest(e.target.value)} minLength={5} maxLength={2000} required />
        </Field>
        <Button type="submit" variant="primary" disabled={busy || running || !enabled || !nodeId || request.trim().length < 5}>
          {running ? "Generating…" : "Generate"}
        </Button>
      </form>
      {data && (
        <div className="mt-4 space-y-3 text-sm">
          <p className="text-xs text-fg-muted">
            {data.model} · {data.node_name} · {data.kind} · {data.status}
            {data.error ? ` · ${message(data.error)}` : ""}
          </p>
          {running && <p className="text-fg-muted">Waiting for the model…</p>}
          {data.warnings.length > 0 && (
            <Alert kind="info">
              <ul className="list-disc space-y-0.5 pl-5">
                {data.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </Alert>
          )}
          {data.content && (
            <div className="space-y-2">
              <pre className="max-h-96 overflow-auto whitespace-pre rounded bg-elevated p-3 font-mono text-xs">{data.content}</pre>
              <Button onClick={() => void navigator.clipboard?.writeText(data.content ?? "")}>Copy</Button>
            </div>
          )}
          {data.notes.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-5 text-fg-secondary">
              {data.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Card>
  );
}
