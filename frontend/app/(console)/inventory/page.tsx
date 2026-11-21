"use client";

import { Download, FolderOpen, Pencil, Plus, Server, SquareTerminal, Trash2 } from "lucide-react";
import Link from "next/link";
import { type FormEvent, useMemo, useState } from "react";

import { EnvBadge } from "@/components/EnvBadge";
import {
  type ServiceState,
  type ServiceType,
  ServicesEditor,
  servicesFrom,
  servicesPayload,
} from "@/components/ServicesEditor";
import { Alert, Button, Card, Field, Modal, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { SortTh } from "@/components/SortTh";
import { type Getters, useSortedRows } from "@/lib/sort";
import { isOlder } from "@/lib/version";
import { RequirePermission, useAuth } from "@/lib/auth";
import { useEnvironments } from "@/lib/environments";
import {
  CAPABILITIES,
  type CapabilityKind,
  type Credential,
  type Group,
  type InventoryNode,
  KIND_LABELS,
  type Tag,
} from "@/lib/inventory";

const SELECT =
  "w-full rounded border border-line bg-canvas px-3 py-1.5 text-sm outline-none focus:border-primary";

function Chip({ children, muted = false }: { children: string; muted?: boolean }) {
  return (
    <span
      className={`inline-block rounded bg-elevated px-1.5 py-0.5 font-mono text-[10px] ${muted ? "text-fg-muted line-through" : "text-fg-secondary"}`}
    >
      {children}
    </span>
  );
}

// ---------------------------------------------------------------- node form
type CapState = {
  on: boolean;
  enabled: boolean;
  credential: string;
  config: Record<string, string | boolean>;
};

const DEFAULT_CONFIG: Record<CapabilityKind, Record<string, string | boolean>> = {
  ssh: { port: "22", username: "", jump_host_id: "" },
  agent: {},
  proxmox: { port: "8006", verify_tls: true, node_name: "" },
  docker: { via: "agent" },
  snmp: { version: "v2c", port: "161", security_level: "authPriv", auth_protocol: "SHA256", priv_protocol: "AES128" },
};

function initialCaps(node: InventoryNode | null): Record<CapabilityKind, CapState> {
  const out = {} as Record<CapabilityKind, CapState>;
  for (const kind of Object.keys(CAPABILITIES) as CapabilityKind[]) {
    const existing = node?.capabilities.find((c) => c.kind === kind);
    const config: Record<string, string | boolean> = { ...DEFAULT_CONFIG[kind] };
    for (const [k, v] of Object.entries(existing?.config ?? {})) {
      config[k] = typeof v === "boolean" ? v : v === null || v === undefined ? "" : String(v);
    }
    out[kind] = {
      on: Boolean(existing),
      enabled: existing?.enabled ?? true,
      credential: existing?.credential?.id ?? "",
      config,
    };
  }
  return out;
}

function toPayload(kind: CapabilityKind, c: CapState) {
  const config: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(c.config)) {
    if (v === "") continue; // optional fields left empty
    config[k] = k === "port" ? Number(v) : v;
  }
  return { kind, enabled: c.enabled, config, credential_id: c.credential || null };
}

function NodeDialog({
  node,
  nodes,
  groups,
  credentials,
  onClose,
  onSaved,
}: {
  node: InventoryNode | null;
  nodes: InventoryNode[];
  groups: Group[];
  credentials: Credential[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(node?.name ?? "");
  const [address, setAddress] = useState(node?.address ?? "");
  const { names: environmentNames } = useEnvironments();
  const [environment, setEnvironment] = useState<string>(node?.environment ?? "");
  const [parent, setParent] = useState(node?.parent?.id ?? "");
  const [description, setDescription] = useState(node?.description ?? "");
  const [tags, setTags] = useState((node?.tags ?? []).join(", "));
  const [picked, setPicked] = useState<string[]>(node?.groups ?? []);
  const [caps, setCaps] = useState(() => initialCaps(node));
  const serviceTypes = useResource<ServiceType[]>("/service-types");
  const [services, setServices] = useState<ServiceState[]>(() => servicesFrom(node?.services ?? []));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const sshHosts = nodes.filter((n) => n.id !== node?.id && n.capabilities.some((c) => c.kind === "ssh" && c.enabled));

  function setCap(kind: CapabilityKind, patch: Partial<CapState>) {
    setCaps((prev) => ({ ...prev, [kind]: { ...prev[kind], ...patch } }));
  }
  function setConfig(kind: CapabilityKind, key: string, value: string | boolean) {
    setCaps((prev) => ({ ...prev, [kind]: { ...prev[kind], config: { ...prev[kind].config, [key]: value } } }));
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const body = {
      name: name.trim(),
      address: address.trim(),
      environment: environment || environmentNames[0],
      parent_id: parent || null,
      description: description.trim() || null,
      tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      groups: picked,
      capabilities: (Object.keys(caps) as CapabilityKind[]).filter((k) => caps[k].on).map((k) => toPayload(k, caps[k])),
      services: servicesPayload(services, serviceTypes.data ?? []),
    };
    const r = node
      ? await api(`/nodes/${node.id}`, { method: "PATCH", body })
      : await api("/nodes", { method: "POST", body });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }

  return (
    <Modal title={node ? `Edit ${node.name}` : "New node"} onClose={onClose} wide>
      <form onSubmit={submit} className="max-h-[75vh] space-y-3 overflow-y-auto pr-1">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name">
            <TextInput autoFocus required value={name} onChange={(e) => setName(e.target.value)} placeholder="pve01" />
          </Field>
          <Field label="IP address or FQDN">
            <TextInput required value={address} onChange={(e) => setAddress(e.target.value)} className="font-mono" />
          </Field>
          <Field label="Environment">
            <select value={environment || environmentNames[0] || ""} onChange={(e) => setEnvironment(e.target.value)} className={SELECT}>
              {environmentNames.map((env) => (
                <option key={env}>{env}</option>
              ))}
            </select>
          </Field>
          <Field label="Depends on (parent)">
            <select value={parent} onChange={(e) => setParent(e.target.value)} className={SELECT}>
              <option value="">—</option>
              {nodes
                .filter((n) => n.id !== node?.id)
                .map((n) => (
                  <option key={n.id} value={n.id}>
                    {n.name}
                  </option>
                ))}
            </select>
          </Field>
        </div>
        <Field label="Description">
          <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Field label="Tags" hint="Comma-separated, e.g. web, nginx">
          <TextInput value={tags} onChange={(e) => setTags(e.target.value)} className="font-mono" />
        </Field>
        {groups.length > 0 && (
          <Field label="Groups">
            <div className="flex flex-wrap gap-3">
              {groups.map((g) => (
                <label key={g.id} className="flex items-center gap-1.5 text-xs">
                  <input
                    type="checkbox"
                    checked={picked.includes(g.name)}
                    onChange={(e) => setPicked(e.target.checked ? [...picked, g.name] : picked.filter((x) => x !== g.name))}
                    className="accent-primary"
                  />
                  {g.name}
                </label>
              ))}
            </div>
          </Field>
        )}

        <div className="space-y-2">
          <span className="text-xs font-medium text-fg-secondary">Capabilities</span>
          {(Object.keys(CAPABILITIES) as CapabilityKind[]).map((kind) => {
            const rule = CAPABILITIES[kind];
            const c = caps[kind];
            const usable = credentials.filter((cr) => rule.credentials.includes(cr.kind));
            return (
              <div key={kind} className={`rounded border p-2 ${c.on ? "border-line-strong bg-elevated/50" : "border-line"}`}>
                <label className="flex items-center gap-2 text-xs font-semibold">
                  <input type="checkbox" checked={c.on} onChange={(e) => setCap(kind, { on: e.target.checked })} className="accent-primary" />
                  {rule.label}
                  {c.on && (
                    <span className="ml-auto flex items-center gap-1 font-normal text-fg-secondary">
                      <input type="checkbox" checked={c.enabled} onChange={(e) => setCap(kind, { enabled: e.target.checked })} className="accent-primary" />
                      enabled
                    </span>
                  )}
                </label>
                {c.on && (
                  <div className="mt-2 grid grid-cols-2 gap-2">
                    {rule.credentials.length > 0 && (
                      <div className="col-span-2">
                        <Field label={`Credential (${rule.credentials.map((k) => KIND_LABELS[k]).join(" or ")})`}>
                          <select value={c.credential} onChange={(e) => setCap(kind, { credential: e.target.value })} className={SELECT}>
                            <option value="">{rule.required ? "— select —" : "—"}</option>
                            {usable.map((cr) => (
                              <option key={cr.id} value={cr.id}>
                                {cr.name}
                                {cr.username ? ` (${cr.username})` : ""}
                              </option>
                            ))}
                          </select>
                        </Field>
                      </div>
                    )}
                    {kind === "ssh" && (
                      <>
                        <Field label="Port">
                          <TextInput inputMode="numeric" value={String(c.config.port)} onChange={(e) => setConfig(kind, "port", e.target.value)} />
                        </Field>
                        <Field label="User override">
                          <TextInput value={String(c.config.username)} onChange={(e) => setConfig(kind, "username", e.target.value)} />
                        </Field>
                        <div className="col-span-2">
                          <Field label="Jump host (ProxyJump)">
                            <select value={String(c.config.jump_host_id)} onChange={(e) => setConfig(kind, "jump_host_id", e.target.value)} className={SELECT}>
                              <option value="">— direct —</option>
                              {sshHosts.map((n) => (
                                <option key={n.id} value={n.id}>
                                  {n.name}
                                </option>
                              ))}
                            </select>
                          </Field>
                        </div>
                      </>
                    )}
                    {kind === "proxmox" && (
                      <>
                        <Field label="API port">
                          <TextInput inputMode="numeric" value={String(c.config.port)} onChange={(e) => setConfig(kind, "port", e.target.value)} />
                        </Field>
                        <Field label="PVE node name">
                          <TextInput value={String(c.config.node_name)} onChange={(e) => setConfig(kind, "node_name", e.target.value)} />
                        </Field>
                        <label className="col-span-2 flex items-center gap-2 text-xs">
                          <input
                            type="checkbox"
                            checked={Boolean(c.config.verify_tls)}
                            onChange={(e) => setConfig(kind, "verify_tls", e.target.checked)}
                            className="accent-primary"
                          />
                          Verify the TLS certificate
                        </label>
                      </>
                    )}
                    {kind === "docker" && (
                      <Field label="Reached via">
                        <select value={String(c.config.via)} onChange={(e) => setConfig(kind, "via", e.target.value)} className={SELECT}>
                          <option value="agent">Janus agent</option>
                          <option value="ssh">SSH</option>
                        </select>
                      </Field>
                    )}
                    {kind === "snmp" && (
                      <>
                        <Field label="Version">
                          <select value={String(c.config.version)} onChange={(e) => setConfig(kind, "version", e.target.value)} className={SELECT}>
                            <option value="v2c">v2c</option>
                            <option value="v3">v3</option>
                          </select>
                        </Field>
                        <Field label="Port">
                          <TextInput inputMode="numeric" value={String(c.config.port)} onChange={(e) => setConfig(kind, "port", e.target.value)} />
                        </Field>
                        {c.config.version === "v3" && (
                          <>
                            <Field label="Security level">
                              <select value={String(c.config.security_level)} onChange={(e) => setConfig(kind, "security_level", e.target.value)} className={SELECT}>
                                <option value="authPriv">authPriv (recommended)</option>
                                <option value="authNoPriv">authNoPriv (no encryption)</option>
                                <option value="noAuthNoPriv">noAuthNoPriv (no security)</option>
                              </select>
                            </Field>
                            <Field label="Authentication">
                              <select value={String(c.config.auth_protocol)} onChange={(e) => setConfig(kind, "auth_protocol", e.target.value)} className={SELECT}>
                                <option value="SHA256">SHA-256</option>
                                <option value="SHA">SHA-1</option>
                              </select>
                            </Field>
                            <Field label="Privacy">
                              <select value={String(c.config.priv_protocol)} onChange={(e) => setConfig(kind, "priv_protocol", e.target.value)} className={SELECT}>
                                <option value="AES128">AES-128</option>
                                <option value="AES256">AES-256</option>
                              </select>
                            </Field>
                          </>
                        )}
                      </>
                    )}
                    {kind === "agent" && (
                      <p className="col-span-2 text-[11px] text-fg-muted">Agent installation arrives with roadmap phase F2.</p>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <ServicesEditor types={serviceTypes.data ?? []} value={services} onChange={setServices} credentials={credentials} />

        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy || !name.trim() || !address.trim()}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------- nodes tab
const NODE_SORT: Getters<InventoryNode> = {
  name: (n) => n.name,
  address: (n) => n.address,
  env: (n) => n.environment,
  caps: (n) => n.capabilities.filter((c) => c.enabled).length,
  services: (n) => n.services.length,
  agent: (n) => (n.agent ? (n.agent.version ?? "?") : null),
  installed: (n) => (n.agent && !n.agent.revoked ? n.agent.install_path : null),
  labels: (n) => [...n.tags, ...n.groups].join(" "),
  parent: (n) => n.parent?.name,
};

function NodesTab({ groups }: { groups: Group[] }) {
  const { can } = useAuth();
  const manage = can("inventory.manage");
  const canSsh = can("ssh.connect");
  const canFiles = can("sftp.transfer");
  const [q, setQ] = useState("");
  const { names: environmentNames } = useEnvironments();
  const [env, setEnv] = useState("");
  const [cap, setCap] = useState("");
  const [group, setGroup] = useState("");
  const { data: nodes, error, reload } = useResource<InventoryNode[]>("/nodes");
  const creds = useResource<Credential[]>("/credentials");
  const [editing, setEditing] = useState<InventoryNode | "new" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (nodes ?? []).filter(
      (n) =>
        (!needle ||
          n.name.toLowerCase().includes(needle) ||
          n.address.toLowerCase().includes(needle) ||
          n.tags.some((t) => t.toLowerCase().includes(needle))) &&
        (!env || n.environment === env) &&
        (!group || n.groups.includes(group)) &&
        (!cap || n.capabilities.some((c) => c.kind === cap && c.enabled)),
    );
  }, [nodes, q, env, cap, group]);
  const { sorted, sort, toggle } = useSortedRows(shown, NODE_SORT);

  const canUpdateAgents = can("agents.manage");
  // the signed agent is the one of the server's own version: an agent below it can be updated
  const server = useResource<{ version: string }>("/health");
  const target = server.data?.version ?? null;
  const outdated = (nodes ?? []).filter((n) => n.agent && !n.agent.revoked && isOlder(n.agent.version, target)).length;
  const [rollout, setRollout] = useState<string | null>(null);

  async function updateFleet() {
    if (!window.confirm("Push the signed agent update to every connected agent that is not on the signed version? Each agent verifies the signature and rolls back if the new version cannot connect.")) return;
    setRollout("Updating…");
    const r = await api<{ node_name: string; result: string; detail: string | null }[]>("/agents/update-all", { method: "POST", body: {} });
    if (!r.ok) return setRollout(message(r.code));
    const count = (k: string) => r.data.filter((x) => x.result === k).length;
    const failed = r.data.filter((x) => x.result === "failed").map((x) => `${x.node_name}: ${message(x.detail ?? "update_failed")}`);
    setRollout(`Updated ${count("updated")}, already current or offline ${count("skipped")}, failed ${count("failed")}.${failed.length ? ` ${failed.join(" · ")}` : ""}`);
    reload();
  }

  async function remove(n: InventoryNode) {
    if (!window.confirm(`Delete node "${n.name}"? Nodes depending on it keep existing, without parent.`)) return;
    const r = await api(`/nodes/${n.id}`, { method: "DELETE" });
    setActionError(r.ok ? null : message(r.code));
    reload();
  }

  return (
    <Card
      title={`Nodes${nodes ? ` (${nodes.length})` : ""}`}
      actions={
        <div className="flex gap-2">
          {canUpdateAgents && (
            <Button
              onClick={() => void updateFleet()}
              title={
                outdated > 0
                  ? `Signed self-update of the connected agents below ${target}`
                  : target
                    ? `Every agent is on ${target} or newer`
                    : "Signed self-update of all connected agents"
              }
              disabled={target !== null && outdated === 0}
            >
              <Download className="h-3.5 w-3.5" aria-hidden />{" "}
              {outdated > 0 ? `Update agents to ${target} (${outdated})` : target ? `Agents up to date (${target})` : "Update agents"}
            </Button>
          )}
          {manage && (
            <Button variant="primary" onClick={() => setEditing("new")}>
              <Plus className="h-3.5 w-3.5" aria-hidden /> New node
            </Button>
          )}
        </div>
      }
    >
      {rollout && <Alert kind="info">{rollout}</Alert>}
      <div className="mb-3 flex flex-wrap gap-2">
        <TextInput placeholder="Search name, address, tag" value={q} onChange={(e) => setQ(e.target.value)} className="max-w-xs" />
        <select value={env} onChange={(e) => setEnv(e.target.value)} className={`${SELECT} max-w-[9rem]`} aria-label="Environment">
          <option value="">All environments</option>
          {environmentNames.map((e) => (
            <option key={e}>{e}</option>
          ))}
        </select>
        <select value={group} onChange={(e) => setGroup(e.target.value)} className={`${SELECT} max-w-[11rem]`} aria-label="Group">
          <option value="">All groups</option>
          {groups.map((g) => (
            <option key={g.id} value={g.name}>
              {g.name}
            </option>
          ))}
        </select>
        <select value={cap} onChange={(e) => setCap(e.target.value)} className={`${SELECT} max-w-[11rem]`} aria-label="Capability">
          <option value="">All capabilities</option>
          {(Object.keys(CAPABILITIES) as CapabilityKind[]).map((k) => (
            <option key={k} value={k}>
              {CAPABILITIES[k].label}
            </option>
          ))}
        </select>
      </div>
      {(error || actionError) && <Alert>{error ?? actionError}</Alert>}
      {nodes && nodes.length === 0 && (
        <p className="text-sm text-fg-muted">The inventory is empty. Add your first node to get started.</p>
      )}
      {shown.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-line text-fg-muted">
              <tr>
                <SortTh label="Node" col="name" sort={sort} onSort={toggle} />
                <SortTh label="Address" col="address" sort={sort} onSort={toggle} />
                <SortTh label="Env" col="env" sort={sort} onSort={toggle} />
                <SortTh label="Capabilities" col="caps" sort={sort} onSort={toggle} />
                <SortTh label="Services" col="services" sort={sort} onSort={toggle} />
                <SortTh label="Agent" col="agent" sort={sort} onSort={toggle} />
                <SortTh label="Installed at" col="installed" sort={sort} onSort={toggle} />
                <SortTh label="Tags / groups" col="labels" sort={sort} onSort={toggle} />
                <SortTh label="Depends on" col="parent" sort={sort} onSort={toggle} />
                <th />
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {sorted.map((n) => (
                <tr key={n.id}>
                  <td className="py-2 pr-3">
                    <Link href={`/nodes/?id=${n.id}`} className="flex items-center gap-1.5 font-semibold hover:text-primary">
                      <Server className="h-3.5 w-3.5 text-telemetry" aria-hidden />
                      {n.name}
                    </Link>
                    {n.description && <div className="text-[11px] text-fg-muted">{n.description}</div>}
                  </td>
                  <td className="py-2 pr-3 font-mono text-fg-secondary">{n.address}</td>
                  <td className="py-2 pr-3">
                    <EnvBadge env={n.environment} />
                  </td>
                  <td className="py-2 pr-3">
                    <div className="flex flex-wrap gap-1">
                      {n.capabilities.map((c) => (
                        <Chip key={c.kind} muted={!c.enabled}>
                          {c.kind}
                        </Chip>
                      ))}
                    </div>
                  </td>
                  <td className="py-2 pr-3">
                    <div className="flex flex-wrap gap-1">
                      {n.services.map((s) => (
                        <Chip key={s.id} muted={!s.enabled}>
                          {s.name === s.kind ? s.kind : `${s.kind}:${s.name}`}
                        </Chip>
                      ))}
                    </div>
                  </td>
                  <td className="py-2 pr-3 font-mono text-[11px]">
                    {n.agent ? (
                      <span
                        className={
                          n.agent.revoked
                            ? "text-fg-muted line-through"
                            : isOlder(n.agent.version, target)
                              ? "font-semibold text-warn"
                              : "text-fg-secondary"
                        }
                        title={!n.agent.revoked && isOlder(n.agent.version, target) ? `Update available: ${target}` : undefined}
                      >
                        {n.agent.version ?? "?"}
                        {n.agent.arch ? ` · ${n.agent.arch}` : ""}
                        {n.agent.revoked ? " · revoked" : ""}
                      </span>
                    ) : (
                      <span className="text-fg-muted">—</span>
                    )}
                  </td>
                  <td className="py-2 pr-3 font-mono text-[11px] text-fg-secondary">
                    {n.agent && !n.agent.revoked ? n.agent.install_path : <span className="text-fg-muted">—</span>}
                  </td>
                  <td className="py-2 pr-3">
                    <div className="flex flex-wrap gap-1">
                      {n.tags.map((t) => (
                        <Chip key={t}>{`#${t}`}</Chip>
                      ))}
                      {n.groups.map((g) => (
                        <Chip key={g}>{`@${g}`}</Chip>
                      ))}
                    </div>
                  </td>
                  <td className="py-2 pr-3 text-fg-secondary">{n.parent?.name ?? ""}</td>
                  <td className="py-2 text-right">
                    <div className="flex justify-end gap-1">
                      {canSsh && n.capabilities.some((c) => c.kind === "ssh" && c.enabled) && (
                        <Link
                          href={`/terminal/?node=${n.id}`}
                          className="inline-flex items-center gap-1 rounded border border-line bg-elevated px-2 py-1.5 text-xs font-semibold text-telemetry hover:border-line-strong"
                          aria-label={`SSH to ${n.name}`}
                        >
                          <SquareTerminal className="h-3 w-3" aria-hidden /> SSH
                        </Link>
                      )}
                      {canFiles && n.capabilities.some((c) => c.kind === "ssh" && c.enabled) && (
                        <Link
                          href={`/terminal/?view=files&node=${n.id}`}
                          className="inline-flex items-center gap-1 rounded border border-line bg-elevated px-2 py-1.5 text-xs font-semibold text-telemetry hover:border-line-strong"
                          aria-label={`Files on ${n.name}`}
                        >
                          <FolderOpen className="h-3 w-3" aria-hidden /> Files
                        </Link>
                      )}
                      {manage && (
                        <>
                        <Button onClick={() => setEditing(n)} aria-label={`Edit ${n.name}`}>
                          <Pencil className="h-3 w-3" aria-hidden />
                        </Button>
                        <Button variant="danger" onClick={() => void remove(n)} aria-label={`Delete ${n.name}`}>
                          <Trash2 className="h-3 w-3" aria-hidden />
                        </Button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editing && nodes && (
        <NodeDialog
          node={editing === "new" ? null : editing}
          nodes={nodes}
          groups={groups}
          credentials={creds.data ?? []}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
            creds.reload();
          }}
        />
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- groups and tags tabs
function GroupDialog({ group, onClose, onSaved }: { group: Group | null; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(group?.name ?? "");
  const [description, setDescription] = useState(group?.description ?? "");
  const [vars, setVars] = useState(JSON.stringify(group?.ansible_vars ?? {}, null, 2));
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    let parsed: unknown;
    try {
      parsed = JSON.parse(vars || "{}");
    } catch {
      return setError("Ansible variables must be a valid JSON object.");
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return setError("Ansible variables must be a JSON object.");
    }
    const body = { name: name.trim(), description: description.trim() || null, ansible_vars: parsed };
    const r = group
      ? await api(`/groups/${group.id}`, { method: "PATCH", body })
      : await api("/groups", { method: "POST", body });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }

  return (
    <Modal title={group ? `Edit group ${group.name}` : "New group"} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Name" hint="Letters, digits, . _ - (also the Ansible group name)">
          <TextInput autoFocus required value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Description">
          <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Field label="Ansible variables (JSON)">
          <textarea
            rows={6}
            spellCheck={false}
            value={vars}
            onChange={(e) => setVars(e.target.value)}
            className="w-full rounded border border-line bg-canvas px-3 py-1.5 font-mono text-[11px] outline-none focus:border-primary"
          />
        </Field>
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={!name.trim()}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}

const GROUP_SORT: Getters<Group> = { name: (g) => g.name, nodes: (g) => g.node_count, vars: (g) => JSON.stringify(g.ansible_vars) };

function GroupsTab({ groups, reload }: { groups: Group[]; reload: () => void }) {
  const { can } = useAuth();
  const manage = can("inventory.manage");
  const [editing, setEditing] = useState<Group | "new" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { sorted, sort, toggle } = useSortedRows(groups, GROUP_SORT);

  async function remove(g: Group) {
    if (!window.confirm(`Delete group "${g.name}"? Its nodes are not deleted.`)) return;
    const r = await api(`/groups/${g.id}`, { method: "DELETE" });
    setError(r.ok ? null : message(r.code));
    reload();
  }

  return (
    <Card
      title={`Groups (${groups.length})`}
      actions={
        manage ? (
          <Button variant="primary" onClick={() => setEditing("new")}>
            <Plus className="h-3.5 w-3.5" aria-hidden /> New group
          </Button>
        ) : null
      }
    >
      {error && <Alert>{error}</Alert>}
      <table className="w-full text-left text-xs">
        <thead className="border-b border-line text-fg-muted">
          <tr>
            <SortTh label="Group" col="name" sort={sort} onSort={toggle} />
            <SortTh label="Nodes" col="nodes" sort={sort} onSort={toggle} />
            <SortTh label="Ansible variables" col="vars" sort={sort} onSort={toggle} />
            <th />
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {sorted.map((g) => (
            <tr key={g.id}>
              <td className="py-2 pr-3">
                <div className="font-semibold">{g.name}</div>
                {g.description && <div className="text-[11px] text-fg-muted">{g.description}</div>}
              </td>
              <td className="py-2 pr-3">{g.node_count}</td>
              <td className="max-w-md truncate py-2 pr-3 font-mono text-fg-muted">{JSON.stringify(g.ansible_vars)}</td>
              <td className="py-2 text-right">
                {manage && (
                  <div className="flex justify-end gap-1">
                    <Button onClick={() => setEditing(g)} aria-label={`Edit ${g.name}`}>
                      <Pencil className="h-3 w-3" aria-hidden />
                    </Button>
                    <Button variant="danger" onClick={() => void remove(g)} aria-label={`Delete ${g.name}`}>
                      <Trash2 className="h-3 w-3" aria-hidden />
                    </Button>
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {editing && (
        <GroupDialog
          group={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
    </Card>
  );
}

function TagsTab() {
  const { can } = useAuth();
  const manage = can("inventory.manage");
  const { data: tags, error, reload } = useResource<Tag[]>("/tags");
  const [actionError, setActionError] = useState<string | null>(null);

  async function rename(t: Tag) {
    const name = window.prompt(`New name for tag "${t.name}"`, t.name)?.trim();
    if (!name || name === t.name) return;
    const r = await api(`/tags/${t.id}`, { method: "PATCH", body: { name } });
    setActionError(r.ok ? null : message(r.code));
    reload();
  }

  async function remove(t: Tag) {
    if (!window.confirm(`Delete tag "${t.name}" from ${t.node_count} node(s)?`)) return;
    const r = await api(`/tags/${t.id}`, { method: "DELETE" });
    setActionError(r.ok ? null : message(r.code));
    reload();
  }

  return (
    <Card title="Tags">
      <p className="mb-3 text-xs text-fg-secondary">Tags are created by typing them on a node.</p>
      {(error || actionError) && <Alert>{error ?? actionError}</Alert>}
      <div className="flex flex-wrap gap-2">
        {(tags ?? []).map((t) => (
          <div key={t.id} className="flex items-center gap-1 rounded border border-line px-2 py-1 text-xs">
            <span className="font-mono">#{t.name}</span>
            <span className="text-fg-muted">{t.node_count}</span>
            {manage && (
              <>
                <button type="button" onClick={() => void rename(t)} aria-label={`Rename ${t.name}`} className="text-fg-muted hover:text-fg">
                  <Pencil className="h-3 w-3" aria-hidden />
                </button>
                <button type="button" onClick={() => void remove(t)} aria-label={`Delete ${t.name}`} className="text-fg-muted hover:text-crit">
                  <Trash2 className="h-3 w-3" aria-hidden />
                </button>
              </>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

function Inventory() {
  const [tab, setTab] = useState<"nodes" | "groups" | "tags">("nodes");
  const groups = useResource<Group[]>("/groups");
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Inventory</h1>
      <div role="tablist" className="flex gap-1 border-b border-line text-xs">
        {(["nodes", "groups", "tags"] as const).map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-3 py-2 capitalize ${tab === t ? "border-primary text-primary" : "border-transparent text-fg-nav hover:text-fg"}`}
          >
            {t}
          </button>
        ))}
      </div>
      {groups.error && <Alert>{groups.error}</Alert>}
      {tab === "nodes" && <NodesTab groups={groups.data ?? []} />}
      {tab === "groups" && <GroupsTab groups={groups.data ?? []} reload={groups.reload} />}
      {tab === "tags" && <TagsTab />}
    </div>
  );
}

export default function InventoryPage() {
  return (
    <RequirePermission permission="inventory.read">
      <Inventory />
    </RequirePermission>
  );
}
