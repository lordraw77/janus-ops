"use client";

import { Field, Select, TextInput } from "@/components/ui";
import { type ChannelOption, type CheckOption, type NodeOption, type Step, changesThings } from "@/lib/workflows";

/** The id and the parameters of one step, for its action. Used by the form editor (one card per
 * step) and by the side panel of the graph editor (the selected node). */
const OPS: Record<string, [string, string][]> = {
  service: [["status", "Check it"], ["start", "Start"], ["stop", "Stop"], ["restart", "Restart"], ["reload", "Reload"]],
  process: [["check", "Check it"], ["kill", "Kill"]],
  container: [["status", "Check it"], ["start", "Start"], ["stop", "Stop"], ["restart", "Restart"]],
  proxmox: [["status", "Check it"], ["start", "Start"], ["shutdown", "Shut down"], ["stop", "Stop (power off)"], ["reboot", "Reboot"], ["snapshot", "Take a snapshot"]],
};

/** The retry and unattended options every step has (an approval has none). */
export function StepOptions({ step: s, onPatch }: { step: Step; onPatch: (patch: Partial<Step>) => void }) {
  if (s.action === "approval") return null;
  const changes = changesThings(s);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-fg-secondary">
        <label className="flex items-center gap-2">
          Try again
          <TextInput type="number" min={0} max={5} value={Number(s.retries ?? 0)} onChange={(e) => onPatch({ retries: Math.max(0, Math.min(5, Number(e.target.value) || 0)) })} className="w-16" aria-label="Retries" />
          times if it fails,
        </label>
        <label className="flex items-center gap-2">
          every
          <TextInput type="number" min={1} max={300} value={Number(s.retry_delay_s ?? 10)} onChange={(e) => onPatch({ retry_delay_s: Number(e.target.value) || 10 })} className="w-20" aria-label="Seconds between retries" disabled={!s.retries} />
          seconds
        </label>
      </div>
      {changes && (
        <label className="flex items-start gap-2 text-xs text-fg-secondary">
          <input type="checkbox" className="mt-0.5" checked={Boolean(s.unattended)} onChange={(e) => onPatch({ unattended: e.target.checked })} />
          <span>
            Run without waiting for approval. <span className="text-fg-muted">This step changes something on a host, so by default a person approves each run. Tick this for an automatic fix (for example a restart started by an alert); every run is still recorded in the audit log.</span>
          </span>
        </label>
      )}
    </div>
  );
}

function NodeSelect({ nodes, value, onChange, label = "Node", hint }: { nodes: NodeOption[]; value: string; onChange: (id: string) => void; label?: string; hint?: string }) {
  return (
    <Field label={label} hint={hint}>
      <Select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Choose a node…</option>
        {nodes.map((n) => (
          <option key={n.id} value={n.id}>
            {n.name} ({n.address})
          </option>
        ))}
      </Select>
    </Field>
  );
}

function Pick({ label, value, options, onChange, hint }: { label: string; value: string; options: [string, string][]; onChange: (v: string) => void; hint?: string }) {
  return (
    <Field label={label} hint={hint}>
      <Select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, text]) => (
          <option key={v} value={v}>
            {text}
          </option>
        ))}
      </Select>
    </Field>
  );
}

export function StepFields({
  step: s,
  nodes,
  channels,
  channelsReadable,
  checks = [],
  onParams,
  onRename,
  compact = false,
}: {
  step: Step;
  nodes: NodeOption[];
  channels: ChannelOption[];
  channelsReadable: boolean;
  /** the checks of the inventory, for the "check state" step */
  checks?: CheckOption[];
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
            <Field label="Message" hint="Quote an earlier step with {{step_id.output}} or {{step_id.status}}.">
              <TextInput value={String(s.params.message ?? "")} maxLength={500} onChange={(e) => onParams({ message: e.target.value })} />
            </Field>
          </>
        )}
        {(s.action === "service" || s.action === "process" || s.action === "container") && (
          <>
            <div className={compact ? "grid gap-2" : "grid gap-2 sm:grid-cols-2"}>
              <NodeSelect nodes={nodes} value={String(s.params.node_id ?? "")} onChange={(v) => onParams({ node_id: v })} hint={s.action === "container" ? "A host with the Docker capability, reached through its agent." : "Its agent must allow commands."} />
              <Pick label="What to do" value={String(s.params.op ?? "")} options={OPS[s.action]} onChange={(v) => onParams({ op: v })} />
            </div>
            <Field
              label={s.action === "service" ? "Service or unit" : s.action === "process" ? "Process name" : "Container name"}
              hint={s.action === "process" ? "The exact name, as pgrep -x sees it (15 characters at most on Linux)." : s.action === "service" ? "A systemd unit (nginx, sshd.service) or a Windows service name." : undefined}
            >
              <TextInput value={String(s.params.name ?? "")} maxLength={128} onChange={(e) => onParams({ name: e.target.value })} className="font-mono" placeholder={s.action === "service" ? "nginx" : s.action === "process" ? "java" : "web"} />
            </Field>
            {(s.params.op === "status" || s.params.op === "check") && (
              <Pick label="The step succeeds when it is" value={String(s.params.expect ?? "running")} options={[["running", "running"], ["stopped", "stopped"]]} onChange={(v) => onParams({ expect: v })} hint="Use a condition on this step to react when it is not." />
            )}
            {s.action === "process" && s.params.op === "kill" && (
              <Pick label="Signal" value={String(s.params.signal ?? "TERM")} options={[["TERM", "TERM (ask it to stop)"], ["HUP", "HUP (reload)"], ["INT", "INT"], ["KILL", "KILL (force)"]]} onChange={(v) => onParams({ signal: v })} hint="Windows always stops the process." />
            )}
          </>
        )}
        {s.action === "proxmox" && (
          <>
            <NodeSelect nodes={nodes} value={String(s.params.cluster ?? "")} onChange={(v) => onParams({ cluster: v })} label="Proxmox host" hint="Any host of the cluster that has the Proxmox capability." />
            <div className={compact ? "grid gap-2" : "grid gap-2 sm:grid-cols-3"}>
              <Field label="Proxmox node name">
                <TextInput value={String(s.params.pve_node ?? "")} maxLength={63} onChange={(e) => onParams({ pve_node: e.target.value })} className="font-mono" placeholder="pve1" />
              </Field>
              <Pick label="Kind" value={String(s.params.kind ?? "qemu")} options={[["qemu", "Virtual machine"], ["lxc", "Container (LXC)"]]} onChange={(v) => onParams({ kind: v })} />
              <Field label="Guest id">
                <TextInput type="number" min={100} value={Number(s.params.vmid ?? 100)} onChange={(e) => onParams({ vmid: Number(e.target.value) })} />
              </Field>
            </div>
            <Pick label="What to do" value={String(s.params.op ?? "status")} options={OPS.proxmox} onChange={(v) => onParams({ op: v })} />
            {s.params.op === "status" && (
              <Pick label="The step succeeds when it is" value={String(s.params.expect ?? "running")} options={[["running", "running"], ["stopped", "stopped"]]} onChange={(v) => onParams({ expect: v })} />
            )}
            {s.params.op === "snapshot" && (
              <Field label="Snapshot name">
                <TextInput value={String(s.params.snapshot ?? "")} maxLength={40} onChange={(e) => onParams({ snapshot: e.target.value })} className="font-mono" placeholder="before-upgrade" />
              </Field>
            )}
            {s.params.op !== "status" && (
              <Field label="Wait for the task (seconds)" hint="0 does not wait: the step succeeds as soon as Proxmox accepts the request.">
                <TextInput type="number" min={0} max={600} value={Number(s.params.wait_s ?? 120)} onChange={(e) => onParams({ wait_s: Number(e.target.value) })} />
              </Field>
            )}
          </>
        )}
        {s.action === "http" && (
          <>
            <NodeSelect nodes={nodes} value={String(s.params.node_id ?? "")} onChange={(v) => onParams({ node_id: v })} hint="The request goes to the address of this node." />
            <div className={compact ? "grid gap-2" : "grid gap-2 sm:grid-cols-4"}>
              <Pick label="Scheme" value={String(s.params.scheme ?? "http")} options={[["http", "http"], ["https", "https"]]} onChange={(v) => onParams({ scheme: v })} />
              <Field label="Port">
                <TextInput type="number" min={1} max={65535} value={Number(s.params.port ?? 80)} onChange={(e) => onParams({ port: Number(e.target.value) })} />
              </Field>
              <Pick label="Method" value={String(s.params.method ?? "GET")} options={[["GET", "GET"], ["HEAD", "HEAD"]]} onChange={(v) => onParams({ method: v })} />
              <Field label="Expected status">
                <TextInput type="number" min={100} max={599} value={Number(s.params.expect_status ?? 200)} onChange={(e) => onParams({ expect_status: Number(e.target.value) })} />
              </Field>
            </div>
            <Field label="Path">
              <TextInput value={String(s.params.path ?? "/")} maxLength={200} onChange={(e) => onParams({ path: e.target.value })} className="font-mono" placeholder="/healthz" />
            </Field>
            <Field label="The answer must contain (optional)" hint="Only with GET.">
              <TextInput value={String(s.params.contains ?? "")} maxLength={200} onChange={(e) => onParams({ contains: e.target.value })} />
            </Field>
            <label className="flex items-center gap-2 text-xs text-fg-secondary">
              <input type="checkbox" checked={Boolean(s.params.verify_tls)} onChange={(e) => onParams({ verify_tls: e.target.checked })} />
              Verify the TLS certificate
            </label>
          </>
        )}
        {s.action === "port" && (
          <>
            <NodeSelect nodes={nodes} value={String(s.params.node_id ?? "")} onChange={(v) => onParams({ node_id: v })} />
            <div className={compact ? "grid gap-2" : "grid gap-2 sm:grid-cols-2"}>
              <Field label="TCP port">
                <TextInput type="number" min={1} max={65535} value={Number(s.params.port ?? 22)} onChange={(e) => onParams({ port: Number(e.target.value) })} />
              </Field>
              <Pick label="The step succeeds when it is" value={String(s.params.expect ?? "open")} options={[["open", "open"], ["closed", "closed"]]} onChange={(v) => onParams({ expect: v })} />
            </div>
          </>
        )}
        {s.action === "wait" && (
          <Field label="Seconds to wait" hint="Up to 600. Use it between a restart and the check that follows.">
            <TextInput type="number" min={1} max={600} value={Number(s.params.seconds ?? 10)} onChange={(e) => onParams({ seconds: Number(e.target.value) })} />
          </Field>
        )}
        {s.action === "check" && (
          <>
            <Field label="Check">
              <Select value={String(s.params.check_id ?? "")} onChange={(e) => onParams({ check_id: e.target.value })}>
                <option value="">Choose a check…</option>
                {checks.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.node_name} — {c.name}
                  </option>
                ))}
              </Select>
            </Field>
            <div className={compact ? "grid gap-2" : "grid gap-2 sm:grid-cols-2"}>
              <Pick label="The step succeeds when it is" value={String(s.params.state ?? "ok")} options={[["ok", "OK"], ["warn", "WARN"], ["crit", "CRIT"], ["unknown", "UNKNOWN"]]} onChange={(v) => onParams({ state: v })} />
              <Field label="Wait up to (seconds)" hint="0 looks once. Otherwise it looks every 5 s until the state is reached.">
                <TextInput type="number" min={0} max={600} value={Number(s.params.wait_s ?? 0)} onChange={(e) => onParams({ wait_s: Number(e.target.value) })} />
              </Field>
            </div>
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
