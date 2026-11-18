"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Alert, Button, Card, Field, Select, TextInput } from "@/components/ui";
import { type AiStatus, type Diagnosis, type DiagnosisAction, type DiagnosisDetail, type OpenProblem } from "@/lib/ai";
import { useLoad } from "@/lib/ansible";
import { api, message } from "@/lib/api";
import { useAuth } from "@/lib/auth";

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
  const pending = (diagnoses.data ?? []).some((d) => d.status === "pending");

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
                />
              )}
            </div>
          )}
        </Card>
      </div>
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
}: {
  detail: DiagnosisDetail;
  canApprove: boolean;
  canExecute: boolean;
  busy: boolean;
  onDecide: (action: DiagnosisAction, change: Record<string, unknown>) => Promise<void>;
  onExecute: (action: DiagnosisAction) => Promise<void>;
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
