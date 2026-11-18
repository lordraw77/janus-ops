"use client";

import { Redo2, Undo2 } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";

import { Alert, Button, Field, Modal, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import dynamic from "next/dynamic";

import { WorkflowEditor } from "@/components/WorkflowEditor";
import { TriggerFields } from "@/components/WorkflowTrigger";
import {
  type ChannelOption,
  type CheckOption,
  type Definition,
  type NodeOption,
  EMPTY_DEFINITION,
  normalize,
  parseDefinition,
  problems,
} from "@/lib/workflows";
import { useAuth } from "@/lib/auth";
import { canRedo, canUndo, fresh, push, redo, undo } from "@/lib/history";

const WorkflowGraph = dynamic(() => import("@/components/WorkflowGraph").then((m) => m.WorkflowGraph), {
  ssr: false,
  loading: () => <p className="p-4 text-xs text-fg-muted">Loading the graph…</p>,
});

type WorkflowRow = {
  id: string;
  name: string;
  description: string;
  definition: Record<string, unknown>;
  enabled: boolean;
  has_webhook?: boolean;
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

/** The webhook secret is shown once: after the workflow gets a webhook trigger or the secret is rotated. */
function SecretDialog({ id, secret, onClose }: { id: string; secret: string; onClose: () => void }) {
  const url = `${typeof window === "undefined" ? "" : window.location.origin}/api/v1/workflows/${id}/webhook`;
  return (
    <Modal title="Webhook secret" onClose={onClose}>
      <div className="space-y-3 text-xs">
        <p className="text-fg-muted">
          POST a JSON object to the URL below, signed with HMAC-SHA256 over the body with this secret, as the header{" "}
          <code className="font-mono">X-Janus-Signature: sha256=&lt;hex&gt;</code>. The body is kept in the run. The secret is shown only now.
        </p>
        <pre className="overflow-x-auto rounded border border-line bg-canvas p-2 font-mono text-[11px]">{url}</pre>
        <pre className="overflow-x-auto rounded border border-line bg-canvas p-2 font-mono text-[11px]">{secret}</pre>
        <div className="flex justify-end">
          <Button type="button" variant="primary" onClick={onClose}>
            Done
          </Button>
        </div>
      </div>
    </Modal>
  );
}

const STATUS_COLOR: Record<string, string> = {
  done: "text-ok",
  failed: "text-crit",
  cancelled: "text-fg-muted",
  waiting_approval: "text-warn",
  running: "text-primary",
  pending: "text-fg-muted",
  skipped: "text-fg-muted",
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

function WorkflowDialog({
  initial,
  onClose,
  onDone,
}: {
  initial?: WorkflowRow;
  onClose: () => void;
  onDone: (saved: { id: string; secret?: string }) => void;
}) {
  const nodes = useResource<NodeOption[]>("/nodes");
  const channels = useResource<ChannelOption[]>("/notifications/channels");
  const checks = useResource<CheckOption[]>("/checks");
  const start = useMemo(() => (initial ? (parseDefinition(JSON.stringify(initial.definition)) ?? EMPTY_DEFINITION) : EMPTY_DEFINITION), [initial]);
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [hist, setHist] = useState(() => fresh(start));
  const def = hist.present;
  const setDef = (next: Definition) => setHist((h) => push(h, next, Date.now()));
  const stepBack = () => setHist((h) => undo(h));
  const stepForward = () => setHist((h) => redo(h));
  const [mode, setMode] = useState<"graph" | "form" | "json">("graph");
  const [json, setJson] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function toJson() {
    setJson(JSON.stringify(normalize(def), null, 2));
    setError(null);
    setMode("json");
  }
  function toTab(next: "graph" | "form" | "json") {
    if (next === mode) return;
    if (next === "json") return toJson();
    if (mode === "json") {
      const parsed = parseDefinition(json);
      if (!parsed) return setError("The JSON is not a workflow definition (it needs a trigger and a list of steps).");
      setDef(parsed);
    }
    setError(null);
    setMode(next);
  }

  // Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y anywhere in the dialog, also after a click on the canvas (focus is then on
  // the page, not in the form); a text field keeps its own undo
  useEffect(() => {
    if (mode === "json") return;
    function onKey(e: globalThis.KeyboardEvent) {
      if (!(e.ctrlKey || e.metaKey)) return;
      if ((e.target as HTMLElement | null)?.closest("input, textarea, select, [contenteditable=true]")) return;
      const k = e.key.toLowerCase();
      if (k === "z" && !e.shiftKey) setHist((h) => undo(h));
      else if ((k === "z" && e.shiftKey) || k === "y") setHist((h) => redo(h));
      else return;
      e.preventDefault();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    let definition: unknown;
    if (mode === "json") {
      try {
        definition = JSON.parse(json);
      } catch {
        return setError("The definition is not valid JSON.");
      }
    } else {
      const found = problems(def);
      if (found.length > 0) return setError(found[0]);
      definition = normalize(def);
    }
    setBusy(true);
    const body = { name: name.trim(), description, definition };
    const r = initial
      ? await api<{ id: string; webhook_secret?: string }>(`/workflows/${initial.id}`, { method: "PATCH", body })
      : await api<{ id: string; webhook_secret?: string }>("/workflows", { method: "POST", body });
    setBusy(false);
    if (!r.ok) return setError(message(r.code) || "The definition was refused: check the trigger and each step's parameters.");
    onDone({ id: r.data.id ?? initial?.id ?? "", secret: r.data.webhook_secret });
  }

  return (
    <Modal title={initial ? `Edit ${initial.name}` : "New workflow"} onClose={onClose} huge>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        {error && <Alert>{error}</Alert>}
        <div className="grid gap-2 sm:grid-cols-2">
          <Field label="Name">
            <TextInput autoFocus required value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Description">
            <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
        </div>
        <div className="flex items-center gap-1 text-xs" role="tablist">
          {(["graph", "form", "json"] as const).map((m) => (
            <button
              key={m}
              type="button"
              role="tab"
              aria-selected={mode === m}
              onClick={() => toTab(m)}
              className={`rounded px-2 py-1 ${mode === m ? "bg-elevated font-semibold text-primary" : "text-fg-muted hover:text-fg"}`}
            >
              {m === "graph" ? "Graph" : m === "form" ? "Form" : "JSON"}
            </button>
          ))}
          {mode !== "json" && (
            <span className="ml-2 flex items-center">
              <button type="button" title="Undo (Ctrl+Z)" aria-label="Undo" disabled={!canUndo(hist)} onClick={stepBack} className="rounded p-1 text-fg-muted hover:bg-elevated hover:text-fg disabled:cursor-not-allowed disabled:opacity-30">
                <Undo2 className="h-3.5 w-3.5" aria-hidden />
              </button>
              <button type="button" title="Redo (Ctrl+Shift+Z)" aria-label="Redo" disabled={!canRedo(hist)} onClick={stepForward} className="rounded p-1 text-fg-muted hover:bg-elevated hover:text-fg disabled:cursor-not-allowed disabled:opacity-30">
                <Redo2 className="h-3.5 w-3.5" aria-hidden />
              </button>
            </span>
          )}
          <span className="ml-2 text-[11px] text-fg-muted">A destructive command and every approval step wait for a person.</span>
        </div>
        <div className="max-h-[68vh] overflow-y-auto pr-1">
          {mode === "graph" && (
            <div className="space-y-3">
              <TriggerFields trigger={def.trigger} onChange={(trigger) => setDef({ ...def, trigger })} checks={checks.data ?? []} />
              <WorkflowGraph
                value={def}
                onChange={setDef}
                nodes={nodes.data ?? []}
                channels={channels.data ?? []}
                channelsReadable={!channels.error}
                height={480}
              />
            </div>
          )}
          {mode === "form" && (
            <WorkflowEditor
              value={def}
              onChange={setDef}
              nodes={nodes.data ?? []}
              channels={channels.data ?? []}
              checks={checks.data ?? []}
              channelsReadable={!channels.error}
            />
          )}
          {mode === "json" && (
            <Field label="Definition (JSON)" hint="A trigger and the steps; the Graph and Form tabs edit the same thing.">
              <textarea
                value={json}
                onChange={(e) => setJson(e.target.value)}
                rows={18}
                spellCheck={false}
                className="w-full rounded border border-line bg-canvas px-3 py-2 font-mono text-[11px] outline-none focus:border-primary"
              />
            </Field>
          )}
        </div>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy || !name.trim()}>
            {initial ? "Save" : "Create"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}


function RunPanel({ runId, definition, onChanged }: { runId: string; definition: Definition | null; onChanged: () => void }) {
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
      {definition && (
        // the flow as it was drawn, each step coloured by how this run went
        <WorkflowGraph
          key={runId}
          readOnly
          value={definition}
          statuses={Object.fromEntries(run.steps.map((s) => [s.step_id, s.status]))}
          height={Math.min(440, 150 + 120 * definition.steps.length)}
        />
      )}
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
  const [editing, setEditing] = useState<WorkflowRow | "new" | null>(null);
  const [secret, setSecret] = useState<{ id: string; value: string } | null>(null);
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

  async function rotate(w: WorkflowRow) {
    if (!window.confirm("Rotate the webhook secret? Senders using the old one stop working.")) return;
    setError(null);
    const r = await api<{ webhook_secret: string }>(`/workflows/${w.id}/rotate-secret`, { method: "POST" });
    if (!r.ok) return setError(message(r.code));
    setSecret({ id: w.id, value: r.data.webhook_secret });
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
          <Button type="button" variant="primary" onClick={() => setEditing("new")}>
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
                  <Button type="button" onClick={() => setEditing(current)}>
                    Edit
                  </Button>
                )}
                {can("workflow.manage") && current.has_webhook && (
                  <Button type="button" onClick={() => void rotate(current)}>
                    Rotate webhook secret
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
              {runId && <RunPanel runId={runId} definition={parseDefinition(JSON.stringify(current.definition))} onChanged={() => runs.reload()} />}
            </>
          )}
        </div>
      </div>
      {editing && (
        <WorkflowDialog
          key={editing === "new" ? "new" : editing.id}
          initial={editing === "new" ? undefined : editing}
          onClose={() => setEditing(null)}
          onDone={(created) => {
            setEditing(null);
            if (created.secret) setSecret({ id: created.id, value: created.secret });
            list.reload();
          }}
        />
      )}
      {secret && <SecretDialog id={secret.id} secret={secret.value} onClose={() => setSecret(null)} />}
    </div>
  );
}
