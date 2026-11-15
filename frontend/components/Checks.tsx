"use client";

import { CalendarClock, Lightbulb, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { type FormEvent, useState } from "react";

import { Alert, Button, Card, Field, Modal, Select, StateBadge, TextInput } from "@/components/ui";
import { ago } from "@/lib/agents";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  type Check,
  type CheckDef,
  type CheckKind,
  DOWNTIME_PRESETS,
  KIND_LABEL,
  type Suggestion,
  describeCheck,
} from "@/lib/monitoring";

const num = (v: FormDataEntryValue | null): number | null => {
  const s = String(v ?? "").trim();
  return s === "" ? null : Number(s);
};
const str = (v: FormDataEntryValue | null): string | undefined => {
  const s = String(v ?? "").trim();
  return s === "" ? undefined : s;
};

/** Create or edit a check; fields follow the kind. */
export function CheckEditor({
  nodeId,
  check,
  onClose,
  onSaved,
}: {
  nodeId: string;
  check: Check | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [kind, setKind] = useState<CheckKind>(check?.kind ?? "metric");
  const [error, setError] = useState<string | null>(null);
  const p = (check?.params ?? {}) as Record<string, unknown>;
  const thresholds = kind === "metric" || kind === "tcp" || kind === "http";

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    let params: Record<string, unknown> = {};
    if (kind === "metric") {
      const labels: Record<string, string> = {};
      for (const pair of String(f.get("labels") ?? "").split(",")) {
        const [k, v] = pair.split("=").map((x) => x.trim());
        if (k && v) labels[k] = v;
      }
      params = { metric: str(f.get("metric")), labels, window_s: num(f.get("window_s")) ?? 0 };
    } else if (kind === "systemd") params = { unit: str(f.get("unit")) };
    else if (kind === "port") params = { port: num(f.get("port")), proto: f.get("proto") };
    else if (kind === "tcp") params = { port: num(f.get("port")), host: str(f.get("host")) };
    else if (kind === "backup") params = { max_age_h: num(f.get("max_age_h")) ?? undefined };
    else if (kind === "http")
      params = {
        url: str(f.get("url")),
        expect_status: str(f.get("expect_status")) ?? "200-399",
        contains: str(f.get("contains")),
        verify_tls: f.get("verify_tls") === "on",
      };
    const body: CheckDef = {
      node_id: nodeId,
      name: String(f.get("name") ?? "").trim(),
      kind,
      params,
      comparison: thresholds ? ((f.get("comparison") as "gt" | "lt") ?? "gt") : null,
      warn_threshold: thresholds ? num(f.get("warn")) : null,
      crit_threshold: thresholds ? num(f.get("crit")) : null,
      check_cycles: num(f.get("cycles")) ?? 1,
      interval_s: num(f.get("interval")) ?? 60,
      enabled: f.get("enabled") === "on",
    };
    if (body.warn_threshold === null && body.crit_threshold === null) body.comparison = null;
    const r = check
      ? await api(`/checks/${check.id}`, { method: "PUT", body })
      : await api("/checks", { method: "POST", body });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }

  return (
    <Modal title={check ? `Edit ${check.name}` : "New check"} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="max-h-[75vh] space-y-3 overflow-y-auto pr-1">
        {error && <Alert>{error}</Alert>}
        <Field label="Kind">
          <Select value={kind} onChange={(e) => setKind(e.target.value as CheckKind)} disabled={!!check}>
            {(Object.keys(KIND_LABEL) as CheckKind[]).map((k) => (
              <option key={k} value={k}>
                {KIND_LABEL[k]}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Name">
          <TextInput name="name" required maxLength={256} defaultValue={check?.name} />
        </Field>
        {kind === "metric" && (
          <>
            <Field label="Metric" hint="e.g. cpu.usage_pct, mem.used_pct, fs.used_pct, load.1, disk.util_pct">
              <TextInput name="metric" required defaultValue={String(p.metric ?? "cpu.usage_pct")} />
            </Field>
            <Field label="Labels" hint="Optional, e.g. mount=/ — empty = all series, the worst one counts">
              <TextInput
                name="labels"
                defaultValue={Object.entries((p.labels as Record<string, string>) ?? {})
                  .map(([k, v]) => `${k}=${v}`)
                  .join(",")}
              />
            </Field>
            <Field label="Average over (seconds)" hint="0 = latest value">
              <TextInput name="window_s" type="number" min={0} max={3600} defaultValue={String(p.window_s ?? 0)} />
            </Field>
          </>
        )}
        {kind === "systemd" && (
          <Field label="Unit" hint="Empty = alert on any failed unit">
            <TextInput name="unit" defaultValue={String(p.unit ?? "")} placeholder="nginx.service" />
          </Field>
        )}
        {(kind === "port" || kind === "tcp") && (
          <div className="grid grid-cols-2 gap-3">
            <Field label="Port">
              <TextInput name="port" type="number" min={1} max={65535} required defaultValue={String(p.port ?? "")} />
            </Field>
            {kind === "port" ? (
              <Field label="Protocol">
                <Select name="proto" defaultValue={String(p.proto ?? "tcp")}>
                  <option>tcp</option>
                  <option>udp</option>
                </Select>
              </Field>
            ) : (
              <Field label="Host" hint="Empty = node address">
                <TextInput name="host" defaultValue={String(p.host ?? "")} />
              </Field>
            )}
          </div>
        )}
        {kind === "backup" && (
          <Field label="Max backup age (hours, empty = per job schedule)">
            <TextInput name="max_age_h" type="number" min={1} max={2160} defaultValue={String(p.max_age_h ?? "")} />
          </Field>
        )}
        {kind === "http" && (
          <>
            <Field label="URL">
              <TextInput name="url" required type="url" defaultValue={String(p.url ?? "https://")} />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Expected status">
                <TextInput name="expect_status" defaultValue={String(p.expect_status ?? "200-399")} />
              </Field>
              <Field label="Body contains">
                <TextInput name="contains" defaultValue={String(p.contains ?? "")} />
              </Field>
            </div>
            <label className="flex items-center gap-2 text-xs text-fg-secondary">
              <input type="checkbox" name="verify_tls" defaultChecked={p.verify_tls !== false} /> Verify the TLS certificate
            </label>
          </>
        )}
        {thresholds && (
          <div className="grid grid-cols-3 gap-3">
            <Field label="Alert when">
              <Select name="comparison" defaultValue={check?.comparison ?? "gt"}>
                <option value="gt">above</option>
                <option value="lt">below</option>
              </Select>
            </Field>
            <Field label={kind === "metric" ? "Warn" : "Warn (ms)"}>
              <TextInput name="warn" type="number" step="any" defaultValue={check?.warn_threshold?.toString() ?? ""} />
            </Field>
            <Field label={kind === "metric" ? "Crit" : "Crit (ms)"}>
              <TextInput name="crit" type="number" step="any" defaultValue={check?.crit_threshold?.toString() ?? ""} />
            </Field>
          </div>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Interval (s)">
            <TextInput name="interval" type="number" min={10} max={86400} defaultValue={String(check?.interval_s ?? 60)} />
          </Field>
          <Field label="Cycles before alerting" hint="Consecutive bad results">
            <TextInput name="cycles" type="number" min={1} max={100} defaultValue={String(check?.check_cycles ?? 3)} />
          </Field>
        </div>
        <label className="flex items-center gap-2 text-xs text-fg-secondary">
          <input type="checkbox" name="enabled" defaultChecked={check?.enabled ?? true} /> Enabled
        </label>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary">
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** Schedule a downtime for a node, a check or a group. */
export function DowntimeDialog({
  target,
  onClose,
  onSaved,
}: {
  target: { node_id?: string; check_id?: string; group_id?: string; label: string };
  onClose: () => void;
  onSaved: () => void;
}) {
  const [minutes, setMinutes] = useState(60);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const startRaw = String(f.get("starts_at") ?? "");
    const start = startRaw ? new Date(startRaw) : new Date();
    const end = new Date(start.getTime() + minutes * 60_000);
    const scope = { node_id: target.node_id, check_id: target.check_id, group_id: target.group_id };
    const r = await api("/downtimes", {
      method: "POST",
      body: {
        ...scope,
        starts_at: startRaw ? start.toISOString() : undefined,
        ends_at: end.toISOString(),
        reason: String(f.get("reason") ?? "").trim(),
      },
    });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }

  return (
    <Modal title={`Downtime for ${target.label}`} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <p className="text-xs text-fg-secondary">
          Problems are still recorded, but no notification is sent until the downtime ends.
        </p>
        {error && <Alert>{error}</Alert>}
        <Field label="Start" hint="Empty = now">
          <TextInput name="starts_at" type="datetime-local" />
        </Field>
        <Field label="Duration">
          <div className="flex flex-wrap gap-1">
            {DOWNTIME_PRESETS.map(([label, m]) => (
              <button
                key={m}
                type="button"
                onClick={() => setMinutes(m)}
                aria-pressed={minutes === m}
                className={`rounded border px-2 py-1 text-xs ${minutes === m ? "border-primary text-primary" : "border-line text-fg-secondary"}`}
              >
                {label}
              </button>
            ))}
            <TextInput
              type="number"
              min={5}
              aria-label="Minutes"
              value={minutes}
              onChange={(e) => setMinutes(Number(e.target.value))}
              className="!w-24"
            />
            <span className="self-center text-xs text-fg-muted">minutes</span>
          </div>
        </Field>
        <Field label="Reason">
          <TextInput name="reason" required maxLength={1000} placeholder="Kernel upgrade" />
        </Field>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary">
            Schedule
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function AckDialog({ label, problemId, onClose, onSaved }: { label: string; problemId: string; onClose: () => void; onSaved: () => void }) {
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const comment = String(new FormData(e.currentTarget).get("comment") ?? "").trim();
    const r = await api(`/problems/${problemId}/ack`, { method: "POST", body: { comment } });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }
  return (
    <Modal title={`Acknowledge ${label}`} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <p className="text-xs text-fg-secondary">
          No more notifications for this problem; a worse state clears the acknowledgement.
        </p>
        {error && <Alert>{error}</Alert>}
        <Field label="Comment">
          <TextInput name="comment" required maxLength={1000} autoFocus placeholder="Looking into it" />
        </Field>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary">
            Acknowledge
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function Flags({ c }: { c: { is_flapping: boolean; unreachable: boolean; in_downtime: boolean; acknowledged_at?: string | null } }) {
  const flags = [
    c.acknowledged_at && ["ACK", "border-telemetry/40 text-telemetry"],
    c.in_downtime && ["DOWNTIME", "border-telemetry/40 text-telemetry"],
    c.unreachable && ["UNREACHABLE", "border-warn/40 text-warn"],
    c.is_flapping && ["FLAPPING", "border-unknown/40 text-unknown"],
  ].filter(Boolean) as [string, string][];
  return (
    <>
      {flags.map(([label, cls]) => (
        <span key={label} className={`ml-1 rounded border px-1 font-mono text-[9px] ${cls}`}>
          {label}
        </span>
      ))}
    </>
  );
}

/** The checks of one node, discovery suggestions and node downtime. */
export function ChecksCard({ nodeId, nodeName }: { nodeId: string; nodeName: string }) {
  const { can } = useAuth();
  const manage = can("monitoring.manage");
  const checks = useResource<Check[]>(`/checks?node_id=${nodeId}`);
  const suggestions = useResource<Suggestion[]>(`/nodes/${nodeId}/discovery`);
  const [editing, setEditing] = useState<Check | "new" | null>(null);
  const [downtime, setDowntime] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = () => {
    checks.reload();
    suggestions.reload();
  };

  async function add(list: Suggestion[]) {
    for (const s of list) {
      const r = await api("/checks", { method: "POST", body: s.check });
      if (!r.ok) {
        setError(message(r.code));
        break;
      }
    }
    reload();
  }

  async function remove(c: Check) {
    if (!window.confirm(`Delete the check "${c.name}" and its history?`)) return;
    const r = await api(`/checks/${c.id}`, { method: "DELETE" });
    setError(r.ok ? null : message(r.code));
    reload();
  }

  async function runNow(c: Check) {
    await api(`/checks/${c.id}/run`, { method: "POST" });
    window.setTimeout(checks.reload, 1500);
  }

  return (
    <Card
      title="Checks"
      actions={
        manage && (
          <div className="flex gap-2">
            <Button onClick={() => setDowntime(true)}>
              <CalendarClock className="h-3 w-3" aria-hidden /> Downtime
            </Button>
            <Button variant="primary" onClick={() => setEditing("new")}>
              <Plus className="h-3 w-3" aria-hidden /> New check
            </Button>
          </div>
        )
      }
    >
      {(error || checks.error) && <Alert>{error ?? checks.error}</Alert>}
      {checks.data?.length === 0 && <p className="text-sm text-fg-muted">No checks yet.</p>}
      <div className="divide-y divide-line">
        {(checks.data ?? []).map((c) => (
          <div key={c.id} className={`flex items-start gap-3 py-2 ${c.enabled ? "" : "opacity-50"}`}>
            <StateBadge state={c.enabled ? c.state : null} soft={c.state_type === "soft"} />
            <div className="min-w-0 flex-1">
              <div className="text-sm">
                {c.name}
                <Flags c={c} />
                {!c.enabled && <span className="ml-2 text-[10px] text-fg-muted">disabled</span>}
              </div>
              <div className="truncate font-mono text-[11px] text-fg-muted">{describeCheck(c)}</div>
              {c.last_output && <div className="truncate text-xs text-fg-secondary">{c.last_output}</div>}
            </div>
            <div className="hidden shrink-0 text-right font-mono text-[10px] text-fg-muted sm:block">
              <div>every {c.interval_s}s × {c.check_cycles}</div>
              <div>{c.last_checked_at ? ago(c.last_checked_at) : "not run yet"}</div>
            </div>
            {manage && (
              <div className="flex shrink-0 gap-1">
                <Button onClick={() => void runNow(c)} aria-label={`Run ${c.name} now`} title="Run now">
                  <Play className="h-3 w-3" aria-hidden />
                </Button>
                <Button onClick={() => setEditing(c)} aria-label={`Edit ${c.name}`}>
                  <Pencil className="h-3 w-3" aria-hidden />
                </Button>
                <Button variant="danger" onClick={() => void remove(c)} aria-label={`Delete ${c.name}`}>
                  <Trash2 className="h-3 w-3" aria-hidden />
                </Button>
              </div>
            )}
          </div>
        ))}
      </div>
      {manage && (suggestions.data?.length ?? 0) > 0 && (
        <div className="mt-3 rounded border border-dashed border-line p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="flex items-center gap-1.5 text-xs font-semibold text-fg-secondary">
              <Lightbulb className="h-3.5 w-3.5 text-warn" aria-hidden /> Suggested checks
            </span>
            <Button onClick={() => void add(suggestions.data ?? [])}>Add all</Button>
          </div>
          <ul className="space-y-1">
            {suggestions.data!.map((s) => (
              <li key={s.check.name} className="flex items-center gap-2 text-xs">
                <span className="min-w-0 flex-1 truncate">
                  {s.check.name}
                  <span className="ml-2 font-mono text-[10px] text-fg-muted">{describeCheck(s.check)}</span>
                </span>
                <Button onClick={() => void add([s])} aria-label={`Add ${s.check.name}`}>
                  <Plus className="h-3 w-3" aria-hidden />
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {editing && (
        <CheckEditor
          nodeId={nodeId}
          check={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
      {downtime && (
        <DowntimeDialog
          target={{ node_id: nodeId, label: nodeName }}
          onClose={() => setDowntime(false)}
          onSaved={() => {
            setDowntime(false);
            reload();
          }}
        />
      )}
    </Card>
  );
}
