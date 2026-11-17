"use client";

import { ArrowDown, ArrowUp, Bell, Clock, Copy, GitBranch, Plus, ShieldCheck, Terminal, Trash2, Zap } from "lucide-react";
import { type ReactNode, useMemo } from "react";

import { Button, Field, Select, TextInput } from "@/components/ui";
import {
  type Action,
  type ChannelOption,
  type CheckOption,
  type Definition,
  type NodeOption,
  type Outcome,
  type Step,
  type Trigger,
  type TriggerType,
  defaultParams,
  freeId,
  problems,
} from "@/lib/workflows";

const ACTIONS: Record<Action, { label: string; icon: ReactNode; tone: string }> = {
  exec: { label: "Run a command", icon: <Terminal className="h-3.5 w-3.5" aria-hidden />, tone: "text-primary" },
  notify: { label: "Send a notification", icon: <Bell className="h-3.5 w-3.5" aria-hidden />, tone: "text-ok" },
  approval: { label: "Wait for approval", icon: <ShieldCheck className="h-3.5 w-3.5" aria-hidden />, tone: "text-warn" },
};

const OUTCOME_LABEL: Record<Outcome, string> = {
  ok: "succeeded",
  failed: "failed",
  skipped: "was skipped",
};

function IconButton({ title, onClick, disabled, children }: { title: string; onClick: () => void; disabled?: boolean; children: ReactNode }) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      disabled={disabled}
      onClick={onClick}
      className="rounded p-1 text-fg-muted hover:bg-elevated hover:text-fg disabled:cursor-not-allowed disabled:opacity-30"
    >
      {children}
    </button>
  );
}

function Connector({ branch }: { branch?: boolean }) {
  return (
    <div className="flex justify-center" aria-hidden>
      <div className={`h-5 border-l-2 ${branch ? "border-dashed border-warn/70" : "border-line"}`} />
    </div>
  );
}

export function WorkflowEditor({
  value,
  onChange,
  nodes,
  channels,
  checks,
  channelsReadable,
}: {
  value: Definition;
  onChange: (next: Definition) => void;
  nodes: NodeOption[];
  channels: ChannelOption[];
  checks: CheckOption[];
  /** false when the user may not list channels: the id is then typed by hand */
  channelsReadable: boolean;
}) {
  const issues = useMemo(() => problems(value), [value]);

  function setTrigger(trigger: Trigger) {
    onChange({ ...value, trigger });
  }
  function setStep(i: number, patch: Partial<Step>) {
    onChange({ ...value, steps: value.steps.map((s, k) => (k === i ? { ...s, ...patch } : s)) });
  }
  function setParams(i: number, patch: Record<string, unknown>) {
    setStep(i, { params: { ...value.steps[i].params, ...patch } });
  }
  function add(action: Action) {
    onChange({ ...value, steps: [...value.steps, { id: freeId(value.steps), action, params: defaultParams(action) }] });
  }
  function remove(i: number) {
    const gone = value.steps[i].id;
    // a condition that pointed at the removed step is dropped with it
    const steps = value.steps.filter((_, k) => k !== i).map((s) => (s.when?.step === gone ? { ...s, when: undefined } : s));
    onChange({ ...value, steps });
  }
  function move(i: number, by: -1 | 1) {
    const j = i + by;
    if (j < 0 || j >= value.steps.length) return;
    const steps = [...value.steps];
    [steps[i], steps[j]] = [steps[j], steps[i]];
    // a condition may only look back: one that now points forward is dropped
    const order = steps.map((s) => s.id);
    onChange({ ...value, steps: steps.map((s, k) => (s.when && order.indexOf(s.when.step) >= k ? { ...s, when: undefined } : s)) });
  }
  function duplicate(i: number) {
    const copy: Step = { ...value.steps[i], id: freeId(value.steps), params: { ...value.steps[i].params } };
    const steps = [...value.steps];
    steps.splice(i + 1, 0, copy);
    onChange({ ...value, steps });
  }
  function rename(i: number, id: string) {
    const old = value.steps[i].id;
    onChange({
      ...value,
      steps: value.steps.map((s, k) => {
        if (k === i) return { ...s, id };
        return s.when?.step === old ? { ...s, when: { ...s.when, step: id } } : s;
      }),
    });
  }

  const t = value.trigger;
  return (
    <div className="space-y-0">
      {/* trigger */}
      <div className="rounded border border-primary/50 bg-elevated p-3">
        <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-primary">
          <Zap className="h-3.5 w-3.5" aria-hidden /> When this starts
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          <Field label="Trigger">
            <Select value={t.type} onChange={(e) => setTrigger({ type: e.target.value as TriggerType })}>
              <option value="manual">By hand (Run now)</option>
              <option value="interval">Every so often</option>
              <option value="alert">When a check raises a problem</option>
              <option value="webhook">When a signed webhook is received</option>
            </Select>
          </Field>
          {t.type === "interval" && (
            <Field label="Every (seconds)" hint="At least 60.">
              <TextInput type="number" min={60} max={86400} value={t.interval_s ?? ""} onChange={(e) => setTrigger({ ...t, interval_s: Number(e.target.value) })} />
            </Field>
          )}
          {t.type === "alert" && (
            <Field label="Check" hint="Empty = any check.">
              <Select value={t.check_id ?? ""} onChange={(e) => setTrigger({ type: "alert", check_id: e.target.value || undefined })}>
                <option value="">Any check</option>
                {checks.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.node_name} · {c.name}
                  </option>
                ))}
              </Select>
            </Field>
          )}
        </div>
        {t.type === "webhook" && (
          <p className="mt-2 text-[11px] text-fg-muted">
            A POST signed with HMAC-SHA256 starts a run; its JSON body is kept in the run. The secret is shown once, when you save.
          </p>
        )}
        {t.type === "alert" && (
          <p className="mt-2 text-[11px] text-fg-muted">The run starts within 30 seconds of the problem opening.</p>
        )}
      </div>

      {/* steps */}
      {value.steps.map((s, i) => {
        const meta = ACTIONS[s.action];
        const earlier = value.steps.slice(0, i);
        const destructive = s.action === "exec" && !s.params.read_only;
        return (
          <div key={`${i}-${s.action}`}>
            <Connector branch={Boolean(s.when)} />
            <div className={`rounded border bg-canvas p-3 ${s.when ? "border-dashed border-warn/70" : "border-line"}`}>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span className="flex h-5 w-5 items-center justify-center rounded-full bg-elevated text-[10px] font-semibold text-fg-secondary">{i + 1}</span>
                <span className={`flex items-center gap-1 text-xs font-semibold ${meta.tone}`}>
                  {meta.icon} {meta.label}
                </span>
                {s.when && (
                  <span className="flex items-center gap-1 rounded border border-warn/50 px-1.5 py-0.5 text-[10px] text-warn">
                    <GitBranch className="h-3 w-3" aria-hidden /> only if {s.when.step} {OUTCOME_LABEL[s.when.outcome]}
                  </span>
                )}
                {(s.action === "approval" || destructive) && (
                  <span className="rounded border border-warn/50 px-1.5 py-0.5 text-[10px] text-warn">waits for a person</span>
                )}
                {s.on_failure === "continue" && (
                  <span className="rounded border border-line px-1.5 py-0.5 text-[10px] text-fg-muted">a failure does not stop the run</span>
                )}
                <span className="ml-auto flex items-center">
                  <IconButton title="Move up" onClick={() => move(i, -1)} disabled={i === 0}>
                    <ArrowUp className="h-3.5 w-3.5" />
                  </IconButton>
                  <IconButton title="Move down" onClick={() => move(i, 1)} disabled={i === value.steps.length - 1}>
                    <ArrowDown className="h-3.5 w-3.5" />
                  </IconButton>
                  <IconButton title="Duplicate" onClick={() => duplicate(i)}>
                    <Copy className="h-3.5 w-3.5" />
                  </IconButton>
                  <IconButton title="Remove" onClick={() => remove(i)}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </IconButton>
                </span>
              </div>

              <div className="grid gap-2 sm:grid-cols-[8rem_1fr]">
                <Field label="Step id">
                  <TextInput value={s.id} maxLength={32} onChange={(e) => rename(i, e.target.value)} className="font-mono" />
                </Field>
                <div className="space-y-2">
                  {s.action === "exec" && (
                    <>
                      <div className="grid gap-2 sm:grid-cols-2">
                        <Field label="Node" hint="Its agent must allow commands.">
                          <Select value={String(s.params.node_id ?? "")} onChange={(e) => setParams(i, { node_id: e.target.value })}>
                            <option value="">Choose a node…</option>
                            {nodes.map((n) => (
                              <option key={n.id} value={n.id}>
                                {n.name} ({n.address})
                              </option>
                            ))}
                          </Select>
                        </Field>
                        <Field label="Timeout (seconds)">
                          <TextInput type="number" min={1} max={300} value={Number(s.params.timeout_s ?? 60)} onChange={(e) => setParams(i, { timeout_s: Number(e.target.value) })} />
                        </Field>
                      </div>
                      <Field label="Command" hint="Run without a shell: no pipes, redirects or variables.">
                        <TextInput value={String(s.params.command ?? "")} maxLength={1000} onChange={(e) => setParams(i, { command: e.target.value })} className="font-mono" placeholder="systemctl restart nginx" />
                      </Field>
                      <label className="flex items-center gap-2 text-xs text-fg-secondary">
                        <input type="checkbox" checked={Boolean(s.params.read_only)} onChange={(e) => setParams(i, { read_only: e.target.checked })} />
                        This command only reads (it does not wait for approval)
                      </label>
                    </>
                  )}
                  {s.action === "notify" && (
                    <>
                      <Field label="Channel">
                        {channelsReadable ? (
                          <Select value={String(s.params.channel_id ?? "")} onChange={(e) => setParams(i, { channel_id: e.target.value })}>
                            <option value="">Choose a channel…</option>
                            {channels.map((c) => (
                              <option key={c.id} value={c.id}>
                                {c.name} ({c.kind}){c.enabled ? "" : " — disabled"}
                              </option>
                            ))}
                          </Select>
                        ) : (
                          <TextInput value={String(s.params.channel_id ?? "")} onChange={(e) => setParams(i, { channel_id: e.target.value })} placeholder="channel id" className="font-mono" />
                        )}
                      </Field>
                      <Field label="Message">
                        <TextInput value={String(s.params.message ?? "")} maxLength={500} onChange={(e) => setParams(i, { message: e.target.value })} />
                      </Field>
                    </>
                  )}
                  {s.action === "approval" && (
                    <Field label="What the approver sees">
                      <TextInput value={String(s.params.reason ?? "")} maxLength={500} onChange={(e) => setParams(i, { reason: e.target.value })} />
                    </Field>
                  )}
                </div>
              </div>

              {/* branching */}
              <div className="mt-2 space-y-2 border-t border-line pt-2">
                {i > 0 && (
                  <label className="flex items-center gap-2 text-xs text-fg-secondary">
                    <input
                      type="checkbox"
                      checked={Boolean(s.when)}
                      onChange={(e) => setStep(i, { when: e.target.checked ? { step: earlier[earlier.length - 1].id, outcome: "ok" } : undefined })}
                    />
                    Run this step only if an earlier step…
                  </label>
                )}
                {s.when && (
                  <div className="grid gap-2 sm:grid-cols-3">
                    <Select value={s.when.step} onChange={(e) => setStep(i, { when: { ...s.when!, step: e.target.value } })} aria-label="Earlier step">
                      {earlier.map((p, k) => (
                        <option key={p.id} value={p.id}>
                          {k + 1}. {p.id}
                        </option>
                      ))}
                    </Select>
                    <Select value={s.when.outcome} onChange={(e) => setStep(i, { when: { ...s.when!, outcome: e.target.value as Outcome } })} aria-label="Outcome">
                      <option value="ok">succeeded</option>
                      <option value="failed">failed</option>
                      <option value="skipped">was skipped</option>
                    </Select>
                    <TextInput value={s.when.contains ?? ""} maxLength={200} onChange={(e) => setStep(i, { when: { ...s.when!, contains: e.target.value } })} placeholder="…and its output contains (optional)" />
                  </div>
                )}
                {s.action !== "approval" && (
                  <label className="flex items-center gap-2 text-xs text-fg-secondary">
                    <input type="checkbox" checked={s.on_failure === "continue"} onChange={(e) => setStep(i, { on_failure: e.target.checked ? "continue" : "fail" })} />
                    If this step fails, carry on (so a later step can react to the failure)
                  </label>
                )}
              </div>
            </div>
          </div>
        );
      })}

      <Connector />
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-1 text-xs text-fg-muted">
          <Plus className="h-3.5 w-3.5" aria-hidden /> Add a step:
        </span>
        <Button onClick={() => add("exec")}>
          <Terminal className="h-3.5 w-3.5" aria-hidden /> Command
        </Button>
        <Button onClick={() => add("notify")}>
          <Bell className="h-3.5 w-3.5" aria-hidden /> Notification
        </Button>
        <Button onClick={() => add("approval")}>
          <ShieldCheck className="h-3.5 w-3.5" aria-hidden /> Approval
        </Button>
        {t.type === "interval" && (
          <span className="ml-auto flex items-center gap-1 text-[11px] text-fg-muted">
            <Clock className="h-3 w-3" aria-hidden /> runs every {Number(t.interval_s) || "…"} s
          </span>
        )}
      </div>

      {issues.length > 0 && (
        <ul className="mt-3 space-y-0.5 rounded border border-warn/40 bg-warn/10 p-2 text-[11px] text-warn" role="status">
          {issues.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
