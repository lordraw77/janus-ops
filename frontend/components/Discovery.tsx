"use client";

import { Play, Plus, Radar, Trash2 } from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";

import { Alert, Button, Card, Field, Modal, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useEnvironments } from "@/lib/environments";
import type { Credential, Group } from "@/lib/inventory";

type Host = {
  id: string;
  address: string;
  port: number;
  hostname: string | null;
  banner: string | null;
  status: "new" | "ignored" | "imported";
  subnet: string | null;
  node_name: string | null;
  first_seen: string;
  last_seen: string;
};

type Subnet = {
  id: string;
  name: string;
  cidr: string;
  port: number;
  interval_minutes: number | null;
  enabled: boolean;
  last_run_at: string | null;
  last_found: number | null;
  last_error: string | null;
};

type ImportResult = { id: string; address: string; node_id: string | null; name: string | null; error: string | null };

const SELECT =
  "w-full rounded border border-line bg-canvas px-3 py-1.5 text-sm outline-none focus:border-primary";
const INTERVALS: [string, string][] = [
  ["", "On demand only"],
  ["15", "Every 15 minutes"],
  ["60", "Every hour"],
  ["360", "Every 6 hours"],
  ["1440", "Every day"],
  ["10080", "Every week"],
];

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "never";
}

// ---------------------------------------------------------------- credential choice
/** Picks an SSH credential of the vault, or creates one on the spot. */
function CredentialPicker({
  credentials,
  value,
  onChange,
  onCreated,
}: {
  credentials: Credential[];
  value: string;
  onChange: (id: string) => void;
  onCreated: () => void;
}) {
  const { can } = useAuth();
  const [creating, setCreating] = useState(false);
  const [kind, setKind] = useState("ssh_key");
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [secret, setSecret] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const ssh = credentials.filter((c) => c.kind === "ssh_key" || c.kind === "ssh_password");

  async function create() {
    setError(null);
    const r = await api<{ id: string }>("/credentials", {
      method: "POST",
      body: {
        kind,
        name: name.trim(),
        username: username.trim() || null,
        secret,
        passphrase: kind === "ssh_key" && passphrase ? passphrase : null,
      },
    });
    if (!r.ok) return setError(message(r.code));
    onCreated();
    onChange(r.data.id);
    setCreating(false);
    setSecret("");
    setPassphrase("");
  }

  return (
    <div className="space-y-2">
      <Field label="SSH credential" hint="Applied to every host you add">
        <select value={value} onChange={(e) => onChange(e.target.value)} className={SELECT} required>
          <option value="">Choose…</option>
          {ssh.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name} ({c.kind === "ssh_key" ? "key" : "password"}
              {c.username ? `, ${c.username}` : ""})
            </option>
          ))}
        </select>
      </Field>
      {can("credentials.manage") && !creating && (
        <Button onClick={() => setCreating(true)}>
          <Plus className="h-3.5 w-3.5" aria-hidden /> New credential
        </Button>
      )}
      {creating && (
        <div className="space-y-2 rounded border border-line p-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Type">
              <select value={kind} onChange={(e) => setKind(e.target.value)} className={SELECT}>
                <option value="ssh_key">SSH private key</option>
                <option value="ssh_password">SSH password</option>
              </select>
            </Field>
            <Field label="Name">
              <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. lan-root-key" />
            </Field>
            <Field label="Username">
              <TextInput value={username} onChange={(e) => setUsername(e.target.value)} />
            </Field>
            {kind === "ssh_key" && (
              <Field label="Key passphrase" hint="If the key has one">
                <TextInput type="password" autoComplete="off" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} />
              </Field>
            )}
          </div>
          <Field label={kind === "ssh_key" ? "Private key" : "Password"}>
            {kind === "ssh_key" ? (
              <textarea
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                rows={5}
                spellCheck={false}
                className={`${SELECT} font-mono text-xs`}
                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
              />
            ) : (
              <TextInput type="password" autoComplete="new-password" value={secret} onChange={(e) => setSecret(e.target.value)} />
            )}
          </Field>
          {error && <Alert>{error}</Alert>}
          <div className="flex justify-end gap-2">
            <Button onClick={() => setCreating(false)}>Cancel</Button>
            <Button variant="primary" disabled={!name.trim() || !secret} onClick={() => void create()}>
              Save credential
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- add to inventory
function ImportDialog({
  hosts,
  groups,
  onClose,
  onDone,
}: {
  hosts: Host[];
  groups: Group[];
  onClose: () => void;
  onDone: () => void;
}) {
  const creds = useResource<Credential[]>("/credentials");
  const { names: environments } = useEnvironments();
  const [credential, setCredential] = useState("");
  const [environment, setEnvironment] = useState("");
  const [username, setUsername] = useState("");
  const [tags, setTags] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<ImportResult[] | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const r = await api<ImportResult[]>("/discovery/hosts/import", {
      method: "POST",
      body: {
        ids: hosts.map((h) => h.id),
        credential_id: credential,
        environment: environment || environments[0],
        username: username.trim() || null,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
        groups: picked,
      },
    });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    setResults(r.data);
    onDone();
  }

  if (results) {
    const failed = results.filter((x) => x.error);
    return (
      <Modal title="Hosts added" onClose={onClose}>
        <p className="text-sm">
          {results.length - failed.length} of {results.length} host(s) added to the inventory.
        </p>
        {failed.length > 0 && (
          <Alert>{failed.map((f) => `${f.address}: ${message(f.error ?? "failed")}`).join(" · ")}</Alert>
        )}
        <div className="mt-3 flex justify-end">
          <Button variant="primary" onClick={onClose}>
            Close
          </Button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title={`Add ${hosts.length} host(s) to the inventory`} onClose={onClose}>
      <form onSubmit={submit} className="max-h-[75vh] space-y-3 overflow-y-auto pr-1">
        <p className="text-xs text-fg-secondary">
          Each host becomes a node named after its DNS name (or its address) with an SSH capability on the port found.
        </p>
        <CredentialPicker credentials={creds.data ?? []} value={credential} onChange={setCredential} onCreated={creds.reload} />
        <div className="grid grid-cols-2 gap-3">
          <Field label="Environment">
            <select value={environment || environments[0] || ""} onChange={(e) => setEnvironment(e.target.value)} className={SELECT}>
              {environments.map((env) => (
                <option key={env}>{env}</option>
              ))}
            </select>
          </Field>
          <Field label="SSH user" hint="Optional: the credential's user otherwise">
            <TextInput value={username} onChange={(e) => setUsername(e.target.value)} className="font-mono" />
          </Field>
        </div>
        <Field label="Tags" hint="Comma-separated">
          <TextInput value={tags} onChange={(e) => setTags(e.target.value)} className="font-mono" />
        </Field>
        {groups.length > 0 && (
          <Field label="Groups">
            <div className="flex flex-wrap gap-2">
              {groups.map((g) => (
                <label key={g.id} className="flex items-center gap-1 text-xs">
                  <input
                    type="checkbox"
                    checked={picked.includes(g.name)}
                    onChange={(e) => setPicked(e.target.checked ? [...picked, g.name] : picked.filter((x) => x !== g.name))}
                  />
                  {g.name}
                </label>
              ))}
            </div>
          </Field>
        )}
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy || !credential}>
            Add to inventory
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------- saved networks
function SubnetDialog({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState("");
  const [cidr, setCidr] = useState("");
  const [port, setPort] = useState("22");
  const [interval, setInterval] = useState("60");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const r = await api("/discovery/subnets", {
      method: "POST",
      body: { name: name.trim(), cidr: cidr.trim(), port: Number(port), interval_minutes: interval ? Number(interval) : null },
    });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }

  return (
    <Modal title="Save a network" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Name">
          <TextInput autoFocus required value={name} onChange={(e) => setName(e.target.value)} placeholder="Office LAN" />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Network (CIDR)">
            <TextInput required value={cidr} onChange={(e) => setCidr(e.target.value)} className="font-mono" placeholder="192.168.1.0/24" />
          </Field>
          <Field label="TCP port">
            <TextInput required type="number" min={1} max={65535} value={port} onChange={(e) => setPort(e.target.value)} className="font-mono" />
          </Field>
        </div>
        <Field label="Scan" hint="New hosts wait in the list below until you add or ignore them">
          <select value={interval} onChange={(e) => setInterval(e.target.value)} className={SELECT}>
            {INTERVALS.map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
        </Field>
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={!name.trim() || !cidr.trim()}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function SubnetsCard({ onScanned }: { onScanned: () => void }) {
  const subnets = useResource<Subnet[]>("/discovery/subnets");
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ kind: "error" | "info"; text: string } | null>(null);

  async function scan(s: Subnet) {
    setBusy(s.id);
    setNote(null);
    const r = await api<{ new: number }>(`/discovery/subnets/${s.id}/scan`, { method: "POST" });
    setBusy(null);
    setNote(r.ok ? { kind: "info", text: `${s.name}: ${r.data.new} new host(s).` } : { kind: "error", text: message(r.code) });
    subnets.reload();
    onScanned();
  }
  async function patch(s: Subnet, body: Record<string, unknown>) {
    const r = await api(`/discovery/subnets/${s.id}`, { method: "PATCH", body });
    if (!r.ok) setNote({ kind: "error", text: message(r.code) });
    subnets.reload();
  }
  async function remove(s: Subnet) {
    if (!window.confirm(`Remove the saved network "${s.name}"? The hosts it found stay in the list.`)) return;
    const r = await api(`/discovery/subnets/${s.id}`, { method: "DELETE" });
    if (!r.ok) setNote({ kind: "error", text: message(r.code) });
    subnets.reload();
  }

  return (
    <Card
      title="Saved networks"
      actions={
        <Button onClick={() => setAdding(true)}>
          <Plus className="h-3.5 w-3.5" aria-hidden /> Save a network
        </Button>
      }
    >
      {subnets.error && <Alert>{subnets.error}</Alert>}
      {note && <Alert kind={note.kind}>{note.text}</Alert>}
      {subnets.data?.length === 0 && <p className="text-sm text-fg-muted">No saved network: scan one above, or save it to scan it on a schedule.</p>}
      {(subnets.data?.length ?? 0) > 0 && (
        <table className="w-full text-left text-xs">
          <thead className="border-b border-line text-fg-muted">
            <tr>
              <th className="py-1 pr-3">Name</th>
              <th className="pr-3">Network</th>
              <th className="pr-3">Schedule</th>
              <th className="pr-3">Last scan</th>
              <th />
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {subnets.data?.map((s) => (
              <tr key={s.id}>
                <td className="py-2 pr-3 font-semibold">{s.name}</td>
                <td className="pr-3 font-mono text-fg-secondary">
                  {s.cidr}:{s.port}
                </td>
                <td className="pr-3">
                  <select
                    value={s.interval_minutes === null ? "" : String(s.interval_minutes)}
                    onChange={(e) => void patch(s, { interval_minutes: e.target.value ? Number(e.target.value) : null })}
                    className="rounded border border-line bg-canvas px-1 py-0.5 text-xs"
                    aria-label={`Schedule of ${s.name}`}
                  >
                    {INTERVALS.map(([v, label]) => (
                      <option key={v} value={v}>
                        {label}
                      </option>
                    ))}
                    {s.interval_minutes !== null && !INTERVALS.some(([v]) => v === String(s.interval_minutes)) && (
                      <option value={String(s.interval_minutes)}>Every {s.interval_minutes} min</option>
                    )}
                  </select>
                  {s.interval_minutes !== null && (
                    <label className="ml-2 inline-flex items-center gap-1">
                      <input type="checkbox" checked={s.enabled} onChange={(e) => void patch(s, { enabled: e.target.checked })} /> on
                    </label>
                  )}
                </td>
                <td className="pr-3 text-fg-secondary">
                  {when(s.last_run_at)}
                  {s.last_found !== null && ` · ${s.last_found} found`}
                  {s.last_error && <span className="text-crit"> · {message(s.last_error)}</span>}
                </td>
                <td className="py-2 text-right">
                  <div className="flex justify-end gap-1">
                    <Button onClick={() => void scan(s)} disabled={busy === s.id} aria-label={`Scan ${s.name} now`}>
                      <Play className="h-3 w-3" aria-hidden /> {busy === s.id ? "Scanning…" : "Scan now"}
                    </Button>
                    <Button variant="danger" onClick={() => void remove(s)} aria-label={`Remove ${s.name}`}>
                      <Trash2 className="h-3 w-3" aria-hidden />
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {adding && (
        <SubnetDialog
          onClose={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            subnets.reload();
          }}
        />
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- the tab
export function DiscoveryTab({ groups }: { groups: Group[] }) {
  const [status, setStatus] = useState<"new" | "ignored" | "imported">("new");
  const hosts = useResource<Host[]>(`/discovery/hosts?status=${status}`);
  const [cidr, setCidr] = useState("");
  const [port, setPort] = useState("22");
  const [scanning, setScanning] = useState(false);
  const [note, setNote] = useState<{ kind: "error" | "info"; text: string } | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);

  const rows = useMemo(() => hosts.data ?? [], [hosts.data]);
  const addable = useMemo(() => rows.filter((h) => h.status !== "imported" && !h.node_name), [rows]);
  const chosen = addable.filter((h) => selected.has(h.id));

  async function scan(e: FormEvent) {
    e.preventDefault();
    setScanning(true);
    setNote(null);
    const r = await api<{ scanned: number; found: number; new: number }>("/discovery/scan", {
      method: "POST",
      body: { cidr: cidr.trim(), port: Number(port) },
    });
    setScanning(false);
    if (!r.ok) return setNote({ kind: "error", text: message(r.code) });
    setNote({ kind: "info", text: `Scanned ${r.data.scanned} addresses: ${r.data.found} answered on port ${port}, ${r.data.new} new.` });
    setStatus("new");
    hosts.reload();
  }

  async function mark(ids: string[], to: "new" | "ignored") {
    const r = await api("/discovery/hosts/status", { method: "POST", body: { ids, status: to } });
    if (!r.ok) setNote({ kind: "error", text: message(r.code) });
    setSelected(new Set());
    hosts.reload();
  }
  async function forget(ids: string[]) {
    await api("/discovery/hosts/delete", { method: "POST", body: { ids } });
    setSelected(new Set());
    hosts.reload();
  }
  function toggle(id: string, on: boolean) {
    const next = new Set(selected);
    if (on) next.add(id);
    else next.delete(id);
    setSelected(next);
  }

  return (
    <div className="space-y-4">
      <Card title="Scan a network">
        <form onSubmit={scan} className="flex flex-wrap items-end gap-3">
          <Field label="Network (CIDR)" hint="IPv4, at most 4096 addresses (a /20)">
            <TextInput required value={cidr} onChange={(e) => setCidr(e.target.value)} className="w-56 font-mono" placeholder="192.168.1.0/24" />
          </Field>
          <Field label="TCP port">
            <TextInput required type="number" min={1} max={65535} value={port} onChange={(e) => setPort(e.target.value)} className="w-24 font-mono" />
          </Field>
          <Button type="submit" variant="primary" disabled={scanning || !cidr.trim()}>
            <Radar className="h-3.5 w-3.5" aria-hidden /> {scanning ? "Scanning…" : "Scan"}
          </Button>
        </form>
        {note && (
          <div className="mt-3">
            <Alert kind={note.kind}>{note.text}</Alert>
          </div>
        )}
      </Card>

      <SubnetsCard onScanned={hosts.reload} />

      <Card
        title={`Hosts found${hosts.data ? ` (${rows.length})` : ""}`}
        actions={
          <div className="flex gap-2">
            {status === "new" && (
              <Button onClick={() => void mark(chosen.map((h) => h.id), "ignored")} disabled={chosen.length === 0}>
                Ignore
              </Button>
            )}
            {status === "ignored" && (
              <Button onClick={() => void mark(chosen.map((h) => h.id), "new")} disabled={chosen.length === 0}>
                Restore
              </Button>
            )}
            <Button variant="primary" onClick={() => setAdding(true)} disabled={chosen.length === 0}>
              <Plus className="h-3.5 w-3.5" aria-hidden /> Add to inventory{chosen.length > 0 ? ` (${chosen.length})` : ""}
            </Button>
          </div>
        }
      >
        <div role="tablist" className="mb-3 flex gap-1 text-xs">
          {(["new", "ignored", "imported"] as const).map((s) => (
            <button
              key={s}
              role="tab"
              aria-selected={status === s}
              onClick={() => {
                setStatus(s);
                setSelected(new Set());
              }}
              className={`rounded px-2 py-1 capitalize ${status === s ? "bg-elevated text-primary" : "text-fg-nav hover:text-fg"}`}
            >
              {s === "imported" ? "added" : s}
            </button>
          ))}
        </div>
        {hosts.error && <Alert>{hosts.error}</Alert>}
        {hosts.data && rows.length === 0 && <p className="text-sm text-fg-muted">Nothing here yet.</p>}
        {rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-line text-fg-muted">
                <tr>
                  <th className="w-6 py-1">
                    <input
                      type="checkbox"
                      aria-label="Select all"
                      checked={addable.length > 0 && chosen.length === addable.length}
                      onChange={(e) => setSelected(e.target.checked ? new Set(addable.map((h) => h.id)) : new Set())}
                    />
                  </th>
                  <th className="pr-3">Address</th>
                  <th className="pr-3">Name (DNS)</th>
                  <th className="pr-3">Service</th>
                  <th className="pr-3">Network</th>
                  <th className="pr-3">Last seen</th>
                  <th />
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {rows.map((h) => (
                  <tr key={h.id}>
                    <td className="py-2">
                      <input
                        type="checkbox"
                        aria-label={`Select ${h.address}`}
                        disabled={h.status === "imported" || Boolean(h.node_name)}
                        checked={selected.has(h.id)}
                        onChange={(e) => toggle(h.id, e.target.checked)}
                      />
                    </td>
                    <td className="pr-3 font-mono">
                      {h.address}
                      {h.port !== 22 && <span className="text-fg-muted">:{h.port}</span>}
                    </td>
                    <td className="pr-3 text-fg-secondary">{h.hostname ?? "—"}</td>
                    <td className="pr-3 font-mono text-fg-secondary">{h.banner ?? `tcp/${h.port}`}</td>
                    <td className="pr-3 text-fg-secondary">{h.subnet ?? "one-off scan"}</td>
                    <td className="pr-3 text-fg-secondary">{when(h.last_seen)}</td>
                    <td className="py-2 text-right">
                      {h.node_name ? (
                        <span className="text-ok">In inventory as {h.node_name}</span>
                      ) : (
                        h.status !== "imported" && (
                          <Button variant="danger" onClick={() => void forget([h.id])} aria-label={`Forget ${h.address}`}>
                            <Trash2 className="h-3 w-3" aria-hidden />
                          </Button>
                        )
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {adding && (
        <ImportDialog
          hosts={chosen}
          groups={groups}
          onClose={() => setAdding(false)}
          onDone={() => {
            setSelected(new Set());
            hosts.reload();
          }}
        />
      )}
    </div>
  );
}
