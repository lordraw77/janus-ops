"use client";

import { ArrowDown, ArrowUp, Clock, Copy, GitBranch, Trash2 } from "lucide-react";
import { type ReactNode, useMemo } from "react";

import { Select, TextInput } from "@/components/ui";
import { ACTION_ICON, ACTION_TONE, AddStepBar } from "@/components/WorkflowActions";
import { StepFields, StepOptions } from "@/components/WorkflowStepFields";
import { TriggerFields } from "@/components/WorkflowTrigger";
import {
  ACTION_LABEL,
  type Action,
  type ChannelOption,
  type CheckOption,
  type Definition,
  type NodeOption,
  type Outcome,
  type Step,
  type Trigger,
  defaultParams,
  freeId,
  problems,
  waitsForPerson,
} from "@/lib/workflows";

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
      <TriggerFields trigger={value.trigger} onChange={setTrigger} checks={checks} />

      {/* steps */}
      {value.steps.map((s, i) => {
        const earlier = value.steps.slice(0, i);
        return (
          <div key={`${i}-${s.action}`}>
            <Connector branch={Boolean(s.when)} />
            <div className={`rounded border bg-canvas p-3 ${s.when ? "border-dashed border-warn/70" : "border-line"}`}>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span className="flex h-5 w-5 items-center justify-center rounded-full bg-elevated text-[10px] font-semibold text-fg-secondary">{i + 1}</span>
                <span className={`flex items-center gap-1 text-xs font-semibold ${ACTION_TONE[s.action]}`}>
                  {ACTION_ICON[s.action]} {ACTION_LABEL[s.action]}
                </span>
                {s.when && (
                  <span className="flex items-center gap-1 rounded border border-warn/50 px-1.5 py-0.5 text-[10px] text-warn">
                    <GitBranch className="h-3 w-3" aria-hidden /> only if {s.when.step} {OUTCOME_LABEL[s.when.outcome]}
                  </span>
                )}
                {waitsForPerson(s) && (
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

              <StepFields
                step={s}
                nodes={nodes}
                channels={channels}
                channelsReadable={channelsReadable}
                checks={checks}
                onParams={(patch) => setParams(i, patch)}
                onRename={(id) => rename(i, id)}
              />

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
                <StepOptions step={s} onPatch={(patch) => setStep(i, patch)} />
              </div>
            </div>
          </div>
        );
      })}

      <Connector />
      <div className="flex flex-wrap items-center gap-2">
        <AddStepBar onAdd={add} />
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
