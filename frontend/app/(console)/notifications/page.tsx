"use client";

import { Pencil, Plus, Send, Trash2 } from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";

import { FilterBar } from "@/components/FilterBar";
import { SortTh } from "@/components/SortTh";
import { Alert, Button, Card, Field, Modal, Select, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { RequirePermission } from "@/lib/auth";
import { useEnvironments } from "@/lib/environments";
import type { Group, InventoryNode } from "@/lib/inventory";
import { type Getters, useSortedRows } from "@/lib/sort";
import {
  CHANNEL_LABEL,
  type Channel,
  type ChannelKind,
  type CheckKind,
  type Delivery,
  KIND_LABEL,
  type Rule,
} from "@/lib/monitoring";

const s = (f: FormData, k: string) => String(f.get(k) ?? "").trim();

/** Secret fields per kind: [name, label, required]. Never displayed once saved. */
const SECRET_FIELDS: Record<ChannelKind, [string, string, boolean][]> = {
  email: [["password", "SMTP password (only with a user)", false]],
  telegram: [["token", "Bot token", true]],
  webhook: [
    ["url", "URL", true],
    ["hmac_key", "HMAC key (X-Janus-Signature)", false],
  ],
  discord: [["url", "Webhook URL", true]],
  slack: [["url", "Incoming webhook URL", true]],
  ntfy: [["token", "Access token (optional)", false]],
};

function ChannelDialog({ channel, onClose, onSaved }: { channel: Channel | null; onClose: () => void; onSaved: () => void }) {
  const [kind, setKind] = useState<ChannelKind>(channel?.kind ?? "email");
  const [error, setError] = useState<string | null>(null);
  const c = (channel?.config ?? {}) as Record<string, unknown>;

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    let config: Record<string, unknown> = {};
    if (kind === "email")
      config = {
        host: s(f, "host"),
        port: Number(s(f, "port") || 25),
        security: s(f, "security"),
        verify_tls: f.get("verify_tls") === "on",
        sender: s(f, "sender"),
        recipients: s(f, "recipients").split(/[,\s]+/).filter(Boolean),
        username: s(f, "username") || undefined,
      };
    else if (kind === "telegram") config = { chat_id: s(f, "chat_id") };
    else if (kind === "discord") config = { username: s(f, "username") || undefined };
    else if (kind === "ntfy") config = { url: s(f, "ntfy_url") || "https://ntfy.sh", topic: s(f, "topic") };
    const secret: Record<string, string> = {};
    for (const [name] of SECRET_FIELDS[kind]) if (s(f, `secret_${name}`)) secret[name] = s(f, `secret_${name}`);
    const replace = !channel || Object.keys(secret).length > 0;
    const body = { name: s(f, "name"), kind, config, enabled: f.get("enabled") === "on", ...(replace ? { secret } : {}) };
    const r = channel
      ? await api(`/notifications/channels/${channel.id}`, { method: "PUT", body })
      : await api("/notifications/channels", { method: "POST", body });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }

  return (
    <Modal title={channel ? `Edit ${channel.name}` : "New channel"} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="max-h-[75vh] space-y-3 overflow-y-auto pr-1">
        {error && <Alert>{error}</Alert>}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name">
            <TextInput name="name" required maxLength={100} defaultValue={channel?.name} />
          </Field>
          <Field label="Kind">
            <Select value={kind} onChange={(e) => setKind(e.target.value as ChannelKind)} disabled={!!channel}>
              {(Object.keys(CHANNEL_LABEL) as ChannelKind[]).map((k) => (
                <option key={k} value={k}>
                  {CHANNEL_LABEL[k]}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        {kind === "email" && (
          <>
            <div className="grid grid-cols-[1fr_6rem] gap-3">
              <Field label="SMTP server">
                <TextInput name="host" required defaultValue={String(c.host ?? "")} />
              </Field>
              <Field label="Port">
                <TextInput name="port" type="number" defaultValue={String(c.port ?? 25)} />
              </Field>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Security">
                <Select name="security" defaultValue={String(c.security ?? "starttls")}>
                  <option value="none">None</option>
                  <option value="starttls">STARTTLS</option>
                  <option value="tls">TLS (SMTPS)</option>
                </Select>
              </Field>
              <Field label="User" hint="Empty = relay without login">
                <TextInput name="username" defaultValue={String(c.username ?? "")} autoComplete="off" />
              </Field>
            </div>
            <label className="flex items-center gap-2 text-xs text-fg-secondary">
              <input type="checkbox" name="verify_tls" defaultChecked={c.verify_tls !== false} /> Verify the server certificate
            </label>
            <Field label="From">
              <TextInput name="sender" type="email" required defaultValue={String(c.sender ?? "")} />
            </Field>
            <Field label="To" hint="Comma-separated addresses">
              <TextInput name="recipients" required defaultValue={((c.recipients as string[]) ?? []).join(", ")} />
            </Field>
          </>
        )}
        {kind === "telegram" && (
          <Field label="Chat id" hint="Numeric id (groups are negative) or @channel">
            <TextInput name="chat_id" required defaultValue={String(c.chat_id ?? "")} />
          </Field>
        )}
        {kind === "discord" && (
          <Field label="Bot name shown in Discord">
            <TextInput name="username" defaultValue={String(c.username ?? "Janus-Ops")} />
          </Field>
        )}
        {kind === "ntfy" && (
          <div className="grid grid-cols-2 gap-3">
            <Field label="Server">
              <TextInput name="ntfy_url" defaultValue={String(c.url ?? "https://ntfy.sh")} />
            </Field>
            <Field label="Topic">
              <TextInput name="topic" required defaultValue={String(c.topic ?? "")} />
            </Field>
          </div>
        )}
        {SECRET_FIELDS[kind].map(([name, label, required]) => (
          <Field key={name} label={label} hint={channel?.has_secret ? "Stored encrypted. Leave empty to keep it." : "Stored encrypted, never shown again."}>
            <TextInput name={`secret_${name}`} type="password" autoComplete="new-password" required={required && !channel} />
          </Field>
        ))}
        <label className="flex items-center gap-2 text-xs text-fg-secondary">
          <input type="checkbox" name="enabled" defaultChecked={channel?.enabled ?? true} /> Enabled
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

function RuleDialog({ rule, channels, onClose, onSaved }: { rule: Rule | null; channels: Channel[]; onClose: () => void; onSaved: () => void }) {
  const { names: environmentNames } = useEnvironments();
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const escalate = Number(s(f, "escalate") || 0);
    const body = {
      name: s(f, "name"),
      channel_id: s(f, "channel"),
      min_state: s(f, "min_state"),
      filter: { environments: f.getAll("env").map(String), check_kinds: f.getAll("kind").map(String) },
      escalate_after_s: escalate > 0 ? escalate * 60 : null,
      position: Number(s(f, "position") || 0),
      enabled: f.get("enabled") === "on",
    };
    const r = rule
      ? await api(`/notifications/rules/${rule.id}`, { method: "PUT", body })
      : await api("/notifications/rules", { method: "POST", body });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }
  return (
    <Modal title={rule ? `Edit ${rule.name}` : "New rule"} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        {error && <Alert>{error}</Alert>}
        <Field label="Name">
          <TextInput name="name" required maxLength={100} defaultValue={rule?.name} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Channel">
            <Select name="channel" defaultValue={rule?.channel_id} required>
              {channels.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="From state">
            <Select name="min_state" defaultValue={rule?.min_state ?? "crit"}>
              <option value="warn">WARN and worse</option>
              <option value="unknown">UNKNOWN and CRIT</option>
              <option value="crit">CRIT only</option>
            </Select>
          </Field>
        </div>
        <Field label="Environments" hint="None selected = all">
          <div className="flex flex-wrap gap-3 text-xs">
            {environmentNames.map((env) => (
              <label key={env} className="flex items-center gap-1">
                <input type="checkbox" name="env" value={env} defaultChecked={rule?.filter.environments?.includes(env)} /> {env}
              </label>
            ))}
          </div>
        </Field>
        <Field label="Check kinds" hint="None selected = all">
          <div className="flex flex-wrap gap-3 text-xs">
            {(Object.keys(KIND_LABEL) as CheckKind[]).map((k) => (
              <label key={k} className="flex items-center gap-1">
                <input type="checkbox" name="kind" value={k} defaultChecked={rule?.filter.check_kinds?.includes(k)} /> {k}
              </label>
            ))}
          </div>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Escalate after (minutes)" hint="Empty = notify at once; else only if still open and not acknowledged">
            <TextInput name="escalate" type="number" min={1} defaultValue={rule?.escalate_after_s ? String(rule.escalate_after_s / 60) : ""} />
          </Field>
          <Field label="Order">
            <TextInput name="position" type="number" min={0} defaultValue={String(rule?.position ?? 0)} />
          </Field>
        </div>
        <label className="flex items-center gap-2 text-xs text-fg-secondary">
          <input type="checkbox" name="enabled" defaultChecked={rule?.enabled ?? true} /> Enabled
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

const DELIVERY_SORT: Getters<Delivery> = {
  time: (d) => d.sent_at,
  status: (d) => d.status,
  event: (d) => (d.event === "recovery" ? "recovery" : (d.state ?? "")),
  target: (d) => `${d.node_name} / ${d.check_name}`,
  channel: (d) => d.channel_name,
  error: (d) => d.error,
};

function NotificationsView() {
  const channels = useResource<Channel[]>("/notifications/channels");
  const rules = useResource<Rule[]>("/notifications/rules");
  const deliveries = useResource<Delivery[]>("/notifications/deliveries?limit=50");
  const nodesRes = useResource<InventoryNode[]>("/nodes");
  const groupsRes = useResource<Group[]>("/groups");
  const [dq, setDq] = useState("");
  const [dHost, setDHost] = useState("");
  const [dGroup, setDGroup] = useState("");
  const [dStatus, setDStatus] = useState("");
  const [dChannel, setDChannel] = useState("");
  const groupNodes = useMemo(() => new Set((nodesRes.data ?? []).filter((n) => n.groups.includes(dGroup)).map((n) => n.name)), [nodesRes.data, dGroup]);
  const sent = useMemo(() => deliveries.data ?? [], [deliveries.data]);
  const dHosts = useMemo(() => [...new Set(sent.map((d) => d.node_name))].sort(), [sent]);
  const dChannels = useMemo(() => [...new Set(sent.map((d) => d.channel_name ?? ""))].filter(Boolean).sort(), [sent]);
  const dShown = useMemo(() => {
    const needle = dq.trim().toLowerCase();
    return sent.filter(
      (d) =>
        (!needle || [d.node_name, d.check_name, d.channel_name ?? "", d.error ?? ""].some((v) => v.toLowerCase().includes(needle))) &&
        (!dHost || d.node_name === dHost) &&
        (!dGroup || groupNodes.has(d.node_name)) &&
        (!dStatus || d.status === dStatus) &&
        (!dChannel || d.channel_name === dChannel),
    );
  }, [sent, dq, dHost, dGroup, dStatus, dChannel, groupNodes]);
  const { sorted: dSorted, sort: dSort, toggle: dToggle } = useSortedRows(dShown, DELIVERY_SORT);
  const [editChannel, setEditChannel] = useState<Channel | "new" | null>(null);
  const [editRule, setEditRule] = useState<Rule | "new" | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  async function test(c: Channel) {
    setNotice({ kind: "ok", text: `Sending a test to ${c.name}…` });
    const r = await api(`/notifications/channels/${c.id}/test`, { method: "POST" });
    setNotice(
      r.ok
        ? { kind: "ok", text: `Test sent to ${c.name}.` }
        : { kind: "error", text: r.code.startsWith("send_failed:") ? `${c.name}: ${r.code.slice(12)}` : message(r.code) },
    );
  }

  async function remove(path: string, label: string, reload: () => void) {
    if (!window.confirm(`Delete ${label}?`)) return;
    const r = await api(path, { method: "DELETE" });
    setNotice(r.ok ? null : { kind: "error", text: message(r.code) });
    reload();
    rules.reload();
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Notifications</h1>
      {notice && <Alert kind={notice.kind}>{notice.text}</Alert>}
      <Card
        title="Channels"
        actions={
          <Button variant="primary" onClick={() => setEditChannel("new")}>
            <Plus className="h-3 w-3" aria-hidden /> New channel
          </Button>
        }
      >
        {channels.error && <Alert>{channels.error}</Alert>}
        {channels.data?.length === 0 && <p className="text-sm text-fg-muted">No channels yet.</p>}
        <div className="divide-y divide-line">
          {(channels.data ?? []).map((c) => (
            <div key={c.id} className={`flex items-center gap-3 py-2 text-sm ${c.enabled ? "" : "opacity-50"}`}>
              <div className="min-w-0 flex-1">
                {c.name} <span className="ml-1 text-[11px] text-fg-muted">{CHANNEL_LABEL[c.kind]}</span>
                <div className="font-mono text-[11px] text-fg-muted">
                  {c.rules} rule{c.rules === 1 ? "" : "s"}
                  {c.has_secret ? " · secret stored" : ""}
                </div>
              </div>
              <Button onClick={() => void test(c)} title="Send a test notification">
                <Send className="h-3 w-3" aria-hidden /> Test
              </Button>
              <Button onClick={() => setEditChannel(c)} aria-label={`Edit ${c.name}`}>
                <Pencil className="h-3 w-3" aria-hidden />
              </Button>
              <Button variant="danger" onClick={() => void remove(`/notifications/channels/${c.id}`, `the channel ${c.name} and its rules`, channels.reload)} aria-label={`Delete ${c.name}`}>
                <Trash2 className="h-3 w-3" aria-hidden />
              </Button>
            </div>
          ))}
        </div>
      </Card>
      <Card
        title="Rules"
        actions={
          <Button variant="primary" onClick={() => setEditRule("new")} disabled={!channels.data?.length}>
            <Plus className="h-3 w-3" aria-hidden /> New rule
          </Button>
        }
      >
        <p className="mb-2 text-xs text-fg-muted">
          A rule sends hard problems to a channel, and the recovery afterwards. Not notified: acknowledged problems,
          downtimes, unreachable nodes (parent down) and flapping checks.
        </p>
        {rules.data?.length === 0 && <p className="text-sm text-fg-muted">No rules: problems are not notified.</p>}
        <div className="divide-y divide-line">
          {(rules.data ?? []).map((r) => (
            <div key={r.id} className={`flex items-center gap-3 py-2 text-sm ${r.enabled ? "" : "opacity-50"}`}>
              <div className="min-w-0 flex-1">
                {r.name} <span className="text-fg-muted">→ {r.channel_name}</span>
                <div className="font-mono text-[11px] text-fg-muted">
                  {r.min_state.toUpperCase()}+
                  {r.filter.environments?.length ? ` · ${r.filter.environments.join(",")}` : " · all environments"}
                  {r.filter.check_kinds?.length ? ` · ${r.filter.check_kinds.join(",")}` : ""}
                  {r.escalate_after_s ? ` · after ${r.escalate_after_s / 60} min` : ""}
                </div>
              </div>
              <Button onClick={() => setEditRule(r)} aria-label={`Edit ${r.name}`}>
                <Pencil className="h-3 w-3" aria-hidden />
              </Button>
              <Button variant="danger" onClick={() => void remove(`/notifications/rules/${r.id}`, `the rule ${r.name}`, rules.reload)} aria-label={`Delete ${r.name}`}>
                <Trash2 className="h-3 w-3" aria-hidden />
              </Button>
            </div>
          ))}
        </div>
      </Card>
      <Card title="Recent deliveries">
        {deliveries.data?.length === 0 && <p className="text-sm text-fg-muted">Nothing sent yet.</p>}
        {sent.length > 0 && (
          <FilterBar>
            <Field label="Search">
              <TextInput placeholder="Host, check, channel, error" value={dq} onChange={(e) => setDq(e.target.value)} />
            </Field>
            <Field label="Host">
              <Select value={dHost} onChange={(e) => setDHost(e.target.value)}>
                <option value="">All hosts</option>
                {dHosts.map((h) => (
                  <option key={h} value={h}>
                    {h}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Group">
              <Select value={dGroup} onChange={(e) => setDGroup(e.target.value)}>
                <option value="">All groups</option>
                {(groupsRes.data ?? []).map((g) => (
                  <option key={g.id} value={g.name}>
                    {g.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Status">
              <Select value={dStatus} onChange={(e) => setDStatus(e.target.value)}>
                <option value="">Any</option>
                <option value="sent">Sent</option>
                <option value="failed">Failed</option>
              </Select>
            </Field>
            <Field label="Channel">
              <Select value={dChannel} onChange={(e) => setDChannel(e.target.value)}>
                <option value="">All channels</option>
                {dChannels.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </Select>
            </Field>
          </FilterBar>
        )}
        {sent.length > 0 && dShown.length === 0 && <p className="mb-2 text-sm text-fg-muted">Nothing matches.</p>}
        <table className="w-full text-xs">
          <thead className="border-b border-line text-left text-fg-muted">
            <tr>
              <SortTh label="Sent" col="time" sort={dSort} onSort={dToggle} className="py-1 pr-2 font-medium" />
              <SortTh label="Status" col="status" sort={dSort} onSort={dToggle} className="py-1 pr-2 font-medium" />
              <SortTh label="Event" col="event" sort={dSort} onSort={dToggle} className="py-1 pr-2 font-medium" />
              <SortTh label="Node / check" col="target" sort={dSort} onSort={dToggle} className="py-1 pr-2 font-medium" />
              <SortTh label="Channel" col="channel" sort={dSort} onSort={dToggle} className="py-1 pr-2 font-medium" />
              <SortTh label="Error" col="error" sort={dSort} onSort={dToggle} className="py-1 font-medium" />
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {dSorted.map((d) => (
              <tr key={d.id}>
                <td className="py-1 pr-2 font-mono text-fg-muted">{new Date(d.sent_at).toLocaleString()}</td>
                <td className={`pr-2 font-mono ${d.status === "sent" ? "text-ok" : "text-crit"}`}>{d.status}</td>
                <td className="pr-2">{d.event === "recovery" ? "RECOVERY" : d.state?.toUpperCase()}</td>
                <td className="pr-2">
                  {d.node_name} / {d.check_name}
                </td>
                <td className="pr-2 text-fg-secondary">{d.channel_name}</td>
                <td className="text-crit">{d.error}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      {editChannel && (
        <ChannelDialog
          channel={editChannel === "new" ? null : editChannel}
          onClose={() => setEditChannel(null)}
          onSaved={() => {
            setEditChannel(null);
            channels.reload();
          }}
        />
      )}
      {editRule && (
        <RuleDialog
          rule={editRule === "new" ? null : editRule}
          channels={channels.data ?? []}
          onClose={() => setEditRule(null)}
          onSaved={() => {
            setEditRule(null);
            rules.reload();
            channels.reload();
          }}
        />
      )}
    </div>
  );
}

export default function NotificationsPage() {
  return (
    <RequirePermission permission="notifications.manage">
      <NotificationsView />
    </RequirePermission>
  );
}
