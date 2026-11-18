"use client";

import { Zap } from "lucide-react";

import { Field, Select, TextInput } from "@/components/ui";
import type { CheckOption, Trigger, TriggerType } from "@/lib/workflows";

/** What starts the workflow. Used by the form editor and above the graph canvas. */
export function TriggerFields({ trigger: t, onChange: setTrigger, checks }: { trigger: Trigger; onChange: (next: Trigger) => void; checks: CheckOption[] }) {
  return (
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
  );
}
