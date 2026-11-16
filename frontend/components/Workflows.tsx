"use client";

import { type FormEvent, useEffect, useState } from "react";

import { Alert, Button, Field, Modal, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";

type WorkflowRow = {
  id: string;
  name: string;
  description: string;
  definition: Record<string, unknown>;
  enabled: boolean;
  last_fired_at: string | null;
};

type RunRow = {
  id: string;
  trigger: string;
  status: string;
  error: string | null;
  requested_by: string;
  started_at: string;
  finished_at: string | null;
};

type RunDetail = RunRow & {
  steps: { step_id: string; position: number; action: string; status: string; output: string; exit_code: number | null }[];
  approvals: { step_id: string; reason: string; status: string; decided_by: string | null }[];
};

const TEMPLATE = {
  trigger: { type: "manual" },
  steps: [
    { id: "hello", action: "notify", params: { message: "Starting the check" } },
    { id: "look", action: "exec", params: { node_id: "<node id>", command: "uptime", read_only: true } },
    { id: "gate", action: "approval", params: { reason: "Continue?" } },
  ],
};

const STATUS_COLOR: Record<string, string> = {
  done: "text-ok",
  failed: "text-crit",
  cancelled: "text-fg-muted",
  waiting_approval: "text-warn",
  running: "text-primary",
  pending: "text-fg-muted",
};

/** Reads a path every few seconds while `active`; null path = nothing to read. */
function useWatch<T>(path: string | null, active: boolean, everyMs: number) {
  const [state, setState] = useState<{ data: T | null; error: string | null }>({ data: null, error: null });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!path) return;
    let cancelled = false;
    void api<T>(path).then((r) => {
      if (!cancelled) setState(r.ok ? { data: r.data, error: null } : { data: null, error: message(r.code) });
    });
    if (!active) return () => void (cancelled = true);
    const timer = setInterval(() => {
      void api<T>(path).then((r) => {
        if (!cancelled && r.ok) setState({ data: r.data, error: null });
      });
    }, everyMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [path, active, everyMs, tick]);
  // no path: nothing is read, so there is nothing to show
  return { ...(path ? state : { data: null, error: null }), reload: () => setTick((t) => t + 1) };
}

function CreateDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [json, setJson] = useState(JSON.stringify(TEMPLATE, null, 2));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    let definition: unknown;
    try {
      definition = JSON.parse(json);
    } catch {
      return setError("The definition is not valid JSON.");
    }
    setBusy(true);
    const r = await api("/workflows", { method: "POST", body: { name: name.trim(), description, definition } });
    setBusy(false);
    if (!r.ok) return setError(message(r.code) || "The definition was refused: check the trigger and each step's parameters.");
    onDone();
  }

  return (
    <Modal title="New workflow" onClose={onClose} wide>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        {error && <Alert>{error}</Alert>}
        <Field label="Name">
          <TextInput autoFocus required value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Description">
          <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Field label="Definition (JSON)" hint="A trigger and the steps. A destructive exec and every approval step wait for a person.">
          <textarea
            value={json}
            onChange={(e) => setJson(e.target.value)}
            rows={14}
            spellCheck={false}
            className="w-full rounded border border-line bg-canvas px-3 py-2 font-mono text-[11px] outline-none focus:border-primary"
          />
        </Field>
        <div className="flex justify-end">
          <Button type="submit" variant="primary" disabled={busy || !name.trim()}>
            Create
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function RunPanel({ runId, onChanged }: { runId: string; onChanged: () => void }) {
  const { can } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const detail = useWatch<RunDetail>(`/workflows/runs/${runId}`, true, 3000);
  const run = detail.data;
  const live = run !== null && (run.status === "running" || run.status === "waiting_approval");

  async function decide(stepId: string, approve: boolean) {
    setError(null);
    const r = await api(`/workflows/runs/${runId}/steps/${stepId}/${approve ? "approve" : "reject"}`, {
      method: "POST",
      body: { reason },
    });
    if (!r.ok) return setError(message(r.code));
    setReason("");
    detail.reload();
    onChanged();
  }

  async function cancel() {
    setError(null);
    const r = await api(`/workflows/runs/${runId}/cancel`, { method: "POST" });
    if (!r.ok) return setError(message(r.code));
    detail.reload();
    onChanged();
  }

  if (!run) return <p className="text-xs text-fg-muted">{detail.error ?? "Loading the run…"}</p>;
  const pending = run.approvals.filter((a) => a.status === "pending");

  return (
    <div className="space-y-3 rounded border border-line p-3">
      {error && <Alert>{error}</Alert>}
      <div className="flex items-center gap-3 text-xs">
        <span className={`font-semibold ${STATUS_COLOR[run.status] ?? ""}`}>{run.status.replace("_", " ")}</span>
        <span className="text-fg-muted">
          {run.trigger} · by {run.requested_by} · {new Date(run.started_at).toLocaleString()}
        </span>
        {live && can("workflow.run") && (
          <button type="button" onClick={() => void cancel()} className="ml-auto text-crit hover:underline">
            cancel run
          </button>
        )}
      </div>
      {run.error && <div className="text-xs text-crit">{run.error}</div>}
      <table className="w-full text-left text-xs">
        <thead className="text-fg-muted">
          <tr>
            <th className="py-1">Step</th>
            <th className="py-1">Action</th>
            <th className="py-1">Status</th>
            <th className="py-1">Output</th>
          </tr>
        </thead>
        <tbody>
          {run.steps.map((s) => (
            <tr key={s.step_id} className="border-t border-line align-top">
              <td className="py-1 font-mono">{s.step_id}</td>
              <td className="py-1">{s.action}</td>
              <td className={`py-1 ${STATUS_COLOR[s.status] ?? ""}`}>{s.status.replace("_", " ")}</td>
              <td className="max-w-[24rem] truncate py-1 font-mono text-[11px]" title={s.output}>
                {s.output || "–"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {pending.length > 0 && (
        <div className="space-y-2 rounded border border-warn/40 p-3">
          <div className="text-xs font-semibold text-warn">Waiting for a decision</div>
          {pending.map((a) => (
            <div key={a.step_id} className="space-y-2 text-xs">
              <div>
                <span className="font-mono">{a.step_id}</span>: {a.reason}
              </div>
              {can("workflow.approve") && (
                <div className="flex gap-2">
                  <TextInput placeholder="reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
                  <Button type="button" variant="primary" onClick={() => void decide(a.step_id, true)}>
                    Approve
                  </Button>
                  <Button type="button" onClick={() => void decide(a.step_id, false)}>
                    Reject
                  </Button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function WorkflowsView() {
  const { can } = useAuth();
  const list = useResource<WorkflowRow[]>("/workflows");
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const current = (list.data ?? []).find((w) => w.id === selected) ?? null;
  const runs = useWatch<RunRow[]>(current ? `/workflows/${current.id}/runs` : null, true, 4000);

  async function start(w: WorkflowRow) {
    setError(null);
    const r = await api<{ run_id: string }>(`/workflows/${w.id}/run`, { method: "POST" });
    if (!r.ok) return setError(message(r.code));
    setRunId(r.data.run_id);
    runs.reload();
  }

  async function remove(w: WorkflowRow) {
    setError(null);
    const r = await api(`/workflows/${w.id}`, { method: "DELETE" });
    if (!r.ok) return setError(message(r.code));
    setSelected(null);
    list.reload();
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Workflows</h1>
        {can("workflow.manage") && (
          <Button type="button" variant="primary" onClick={() => setCreating(true)}>
            New workflow
          </Button>
        )}
      </div>
      {error && <Alert>{error}</Alert>}
      {list.error && <Alert>{list.error}</Alert>}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,20rem)_1fr]">
        <div className="space-y-1">
          {(list.data ?? []).length === 0 && <p className="text-xs text-fg-muted">No workflows yet.</p>}
          {(list.data ?? []).map((w) => (
            <button
              key={w.id}
              type="button"
              onClick={() => {
                setSelected(w.id);
                setRunId(null);
              }}
              className={`block w-full rounded border p-2 text-left text-xs ${
                selected === w.id ? "border-primary" : "border-line"
              }`}
            >
              <div className="font-semibold">{w.name}</div>
              <div className="text-fg-muted">
                {String((w.definition.trigger as { type?: string }).type ?? "")} trigger ·{" "}
                {(w.definition.steps as unknown[]).length} steps{w.enabled ? "" : " · disabled"}
              </div>
            </button>
          ))}
        </div>
        <div className="space-y-4">
          {!current && <p className="text-xs text-fg-muted">Choose a workflow to see its runs.</p>}
          {current && (
            <>
              <div className="flex flex-wrap items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-semibold">{current.name}</div>
                  {current.description && <div className="text-xs text-fg-muted">{current.description}</div>}
                </div>
                {can("workflow.run") && (
                  <Button type="button" variant="primary" disabled={!current.enabled} onClick={() => void start(current)}>
                    Run now
                  </Button>
                )}
                {can("workflow.manage") && (
                  <button type="button" onClick={() => void remove(current)} className="text-xs text-crit hover:underline">
                    delete
                  </button>
                )}
              </div>
              <details className="text-xs">
                <summary className="cursor-pointer text-fg-muted">Definition</summary>
                <pre className="mt-2 overflow-x-auto rounded border border-line bg-canvas p-2 font-mono text-[11px]">
                  {JSON.stringify(current.definition, null, 2)}
                </pre>
              </details>
              <div className="text-xs font-semibold">Runs</div>
              <div className="space-y-1">
                {(runs.data ?? []).length === 0 && <p className="text-xs text-fg-muted">No runs yet.</p>}
                {(runs.data ?? []).map((r) => (
                  <button
                    key={r.id}
                    type="button"
                    onClick={() => setRunId(r.id)}
                    className={`flex w-full items-center gap-3 rounded border px-2 py-1 text-left text-xs ${
                      runId === r.id ? "border-primary" : "border-line"
                    }`}
                  >
                    <span className={`w-28 font-semibold ${STATUS_COLOR[r.status] ?? ""}`}>
                      {r.status.replace("_", " ")}
                    </span>
                    <span className="text-fg-muted">{r.trigger}</span>
                    <span className="ml-auto text-fg-muted">{new Date(r.started_at).toLocaleString()}</span>
                  </button>
                ))}
              </div>
              {runId && <RunPanel runId={runId} onChanged={() => runs.reload()} />}
            </>
          )}
        </div>
      </div>
      {creating && (
        <CreateDialog
          onClose={() => setCreating(false)}
          onDone={() => {
            setCreating(false);
            list.reload();
          }}
        />
      )}
    </div>
  );
}
