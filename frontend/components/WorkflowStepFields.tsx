"use client";

import { Field, Select, TextInput } from "@/components/ui";
import type { ChannelOption, NodeOption, Step } from "@/lib/workflows";

/** The id and the parameters of one step, for its action. Used by the form editor (one card per
 * step) and by the side panel of the graph editor (the selected node). */
export function StepFields({
  step: s,
  nodes,
  channels,
  channelsReadable,
  onParams,
  onRename,
  compact = false,
}: {
  step: Step;
  nodes: NodeOption[];
  channels: ChannelOption[];
  channelsReadable: boolean;
  onParams: (patch: Record<string, unknown>) => void;
  onRename: (id: string) => void;
  /** one column, for the side panel of the graph editor */
  compact?: boolean;
}) {
  return (
    <div className={compact ? "grid gap-2" : "grid gap-2 sm:grid-cols-[8rem_1fr]"}>
      <Field label="Step id">
        <TextInput value={s.id} maxLength={32} onChange={(e) => onRename(e.target.value)} className="font-mono" />
      </Field>
      <div className="space-y-2">
        {s.action === "exec" && (
          <>
            <div className={compact ? "grid gap-2" : "grid gap-2 sm:grid-cols-2"}>
              <Field label="Node" hint="Its agent must allow commands.">
                <Select value={String(s.params.node_id ?? "")} onChange={(e) => onParams({ node_id: e.target.value })}>
                  <option value="">Choose a node…</option>
                  {nodes.map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.name} ({n.address})
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Timeout (seconds)">
                <TextInput type="number" min={1} max={300} value={Number(s.params.timeout_s ?? 60)} onChange={(e) => onParams({ timeout_s: Number(e.target.value) })} />
              </Field>
            </div>
            <Field label="Command" hint="Run without a shell: no pipes, redirects or variables.">
              <TextInput value={String(s.params.command ?? "")} maxLength={1000} onChange={(e) => onParams({ command: e.target.value })} className="font-mono" placeholder="systemctl restart nginx" />
            </Field>
            <label className="flex items-center gap-2 text-xs text-fg-secondary">
              <input type="checkbox" checked={Boolean(s.params.read_only)} onChange={(e) => onParams({ read_only: e.target.checked })} />
              This command only reads (it does not wait for approval)
            </label>
          </>
        )}
        {s.action === "notify" && (
          <>
            <Field label="Channel">
              {channelsReadable ? (
                <Select value={String(s.params.channel_id ?? "")} onChange={(e) => onParams({ channel_id: e.target.value })}>
                  <option value="">Choose a channel…</option>
                  {channels.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name} ({c.kind}){c.enabled ? "" : " — disabled"}
                    </option>
                  ))}
                </Select>
              ) : (
                <TextInput value={String(s.params.channel_id ?? "")} onChange={(e) => onParams({ channel_id: e.target.value })} placeholder="channel id" className="font-mono" />
              )}
            </Field>
            <Field label="Message">
              <TextInput value={String(s.params.message ?? "")} maxLength={500} onChange={(e) => onParams({ message: e.target.value })} />
            </Field>
          </>
        )}
        {s.action === "approval" && (
          <Field label="What the approver sees">
            <TextInput value={String(s.params.reason ?? "")} maxLength={500} onChange={(e) => onParams({ reason: e.target.value })} />
          </Field>
        )}
      </div>
    </div>
  );
}
