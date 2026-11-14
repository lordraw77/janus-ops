"use client";

import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Camera,
  Copy,
  ExternalLink,
  Info,
  MoveRight,
  Pause,
  Play,
  Power,
  RefreshCw,
  RotateCcw,
  RotateCw,
  Search,
  Square,
  SquareTerminal,
  Trash2,
} from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import type { TerminalStatus } from "@/components/TerminalView";
import { Alert, Button, Card, Field, Modal, Select, TextInput } from "@/components/ui";
import { formatBytes, formatDuration } from "@/lib/agents";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  BACKUP_STYLE,
  type BackupAudit,
  type Cluster,
  type Guest,
  type GuestDetail,
  type Overview,
  type Started,
  TASK_LABEL,
  type Task,
  type TaskDetail,
  pct,
  when,
} from "@/lib/proxmox";

// xterm.js touches `window`: load it in the browser only
const TerminalView = dynamic(() => import("@/components/TerminalView").then((m) => m.TerminalView), { ssr: false });

type Tab = "guests" | "nodes" | "backups" | "tasks";
type Tracked = Started & { label: string; detail?: TaskDetail; error?: string };
type ConsoleTarget = { pvenode: string; kind: "qemu" | "lxc" | "node"; vmid?: number; title: string };
type SortKey = "vmid" | "name" | "node" | "status" | "cpu" | "mem" | "uptime";

const STATUS_BADGE: Record<string, string> = {
  running: "border-ok/40 bg-ok/15 text-ok",
  online: "border-ok/40 bg-ok/15 text-ok",
  paused: "border-warn/40 bg-warn/15 text-warn",
  suspended: "border-warn/40 bg-warn/15 text-warn",
  offline: "border-crit/40 bg-crit/15 text-crit",
};

function Badge({ status }: { status: string }) {
  return (
    <span className={`inline-block rounded border px-1.5 py-0.5 text-[11px] font-semibold ${STATUS_BADGE[status] ?? "border-line bg-elevated text-fg-muted"}`}>
      {status}
    </span>
  );
}

function Bar({ value, label }: { value: number | null; label?: string }) {
  const color = value === null ? "var(--color-offline)" : value >= 90 ? "var(--color-crit)" : value >= 75 ? "var(--color-warn)" : "var(--color-primary)";
  return (
    <div className="min-w-24">
      <div className="h-1.5 overflow-hidden rounded-full bg-elevated" role="meter" aria-valuenow={value ?? 0} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
        <div className="h-full rounded-full" style={{ width: `${value ?? 0}%`, background: color }} />
      </div>
    </div>
  );
}

function IconButton({ title, onClick, disabled, danger, children }: { title: string; onClick: () => void; disabled?: boolean; danger?: boolean; children: ReactNode }) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onClick={onClick}
      disabled={disabled}
      className={`rounded p-1 disabled:opacity-40 ${danger ? "text-fg-muted hover:bg-crit/15 hover:text-crit" : "text-fg-muted hover:bg-elevated hover:text-fg"}`}
    >
      {children}
    </button>
  );
}

function taskLabel(type: string, id: string): string {
  return `${TASK_LABEL[type] ?? type}${id ? ` ${id}` : ""}`;
}

// ---------------------------------------------------------------- tasks

function TaskLog({ clusterKey, task, onClose }: { clusterKey: string; task: { node: string; upid: string; label: string }; onClose: () => void }) {
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLPreElement>(null);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      const r = await api<TaskDetail>(`/proxmox/${clusterKey}/tasks/${task.node}/${encodeURIComponent(task.upid)}`);
      if (cancelled) return;
      if (!r.ok) return setError(message(r.code));
      setDetail(r.data);
      if (r.data.status === "running") timer = setTimeout(() => void load(), 2000);
    };
    void load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [clusterKey, task.node, task.upid]);
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [detail]);
  return (
    <Modal title={`Task · ${task.label}`} onClose={onClose} wide>
      {error && <Alert>{error}</Alert>}
      {detail && (
        <p className="mb-2 font-mono text-[11px] text-fg-muted">
          {task.node} · started {when(detail.starttime)} · {detail.status === "running" ? "running…" : detail.exitstatus}
        </p>
      )}
      <pre ref={box} className="h-[55vh] overflow-auto rounded bg-canvas p-2 font-mono text-[11px] leading-snug text-fg-secondary">
        {detail ? detail.log.join("\n") : "Loading…"}
      </pre>
    </Modal>
  );
}

function TaskTray({ clusterKey, tasks, onDone, onOpen, onDismiss }: { clusterKey: string; tasks: Tracked[]; onDone: () => void; onOpen: (t: Tracked) => void; onDismiss: (upid: string) => void }) {
  const [, setTick] = useState(0);
  const running = tasks.filter((t) => !t.detail || t.detail.status === "running");
  const runningKey = running.map((t) => t.upid).join(",");
  useEffect(() => {
    if (!runningKey) return;
    let cancelled = false;
    const timer = setInterval(async () => {
      let finished = false;
      for (const t of tasks) {
        if (t.detail && t.detail.status !== "running") continue;
        const r = await api<TaskDetail>(`/proxmox/${clusterKey}/tasks/${t.node}/${encodeURIComponent(t.upid)}`);
        if (cancelled) return;
        if (r.ok) {
          t.detail = r.data;
          if (r.data.status !== "running") finished = true;
        } else t.error = message(r.code);
      }
      setTick((x) => x + 1);
      if (finished) onDone();
    }, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [clusterKey, runningKey, tasks, onDone]);
  if (!tasks.length) return null;
  return (
    <div className="space-y-1">
      {tasks.map((t) => {
        const done = t.detail && t.detail.status !== "running";
        const ok = done && t.detail?.exitstatus === "OK";
        return (
          <div key={t.upid} className={`flex items-center gap-2 rounded border px-3 py-1.5 text-xs ${!done ? "border-line bg-surface" : ok ? "border-ok/40 bg-ok/10" : "border-crit/40 bg-crit/10"}`}>
            {!done ? <RefreshCw className="h-3 w-3 animate-spin text-fg-muted" aria-hidden /> : null}
            <span className="font-semibold">{t.label}</span>
            <span className="font-mono text-fg-muted">{!done ? "running…" : ok ? "done" : t.detail?.exitstatus ?? t.error}</span>
            <span className="ml-auto flex gap-2">
              <button type="button" className="text-fg-muted hover:text-fg" onClick={() => onOpen(t)}>
                log
              </button>
              {done && (
                <button type="button" className="text-fg-muted hover:text-fg" onClick={() => onDismiss(t.upid)} aria-label="Dismiss">
                  ✕
                </button>
              )}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------- console

function ConsoleDialog({ clusterKey, target, onClose }: { clusterKey: string; target: ConsoleTarget; onClose: () => void }) {
  const [status, setStatus] = useState<TerminalStatus>({ state: "connecting" });
  const params: Record<string, string> = { pvenode: target.pvenode, kind: target.kind };
  if (target.vmid) params.vmid = String(target.vmid);
  return (
    <Modal title={`Console · ${target.title}`} onClose={onClose} wide>
      <div>
        <div className="mb-1 font-mono text-[10px] text-fg-muted">
          {status.state === "connected" ? `connected · ${status.user}` : status.state === "error" ? message(status.code) : status.state === "closed" ? `closed (${status.reason})` : "connecting…"}
          {" · recorded in the audit log"}
        </div>
        <div className="h-[60vh] rounded bg-canvas p-1">
          <TerminalView nodeId={clusterKey} active onStatus={setStatus} wsPath="/api/v1/proxmox/console/ws" extraParams={params} />
        </div>
        {target.kind === "qemu" && <p className="mt-1 text-[10px] text-fg-muted">VM consoles use the serial port (serial0 = socket): the guest needs a getty on ttyS0.</p>}
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- guest detail

function GuestDialog({
  clusterKey,
  guest,
  nodes,
  onClose,
  onStarted,
  onConsole,
}: {
  clusterKey: string;
  guest: Guest;
  nodes: string[];
  onClose: () => void;
  onStarted: (s: Started, label: string) => void;
  onConsole: () => void;
}) {
  const { can } = useAuth();
  const manage = can("proxmox.manage");
  const base = `/proxmox/${clusterKey}/guests/${guest.node}/${guest.type}/${guest.vmid}`;
  const detail = useResource<GuestDetail>(base);
  const [error, setError] = useState<string | null>(null);
  const [snap, setSnap] = useState({ name: "", description: "", vmstate: false });
  const [clone, setClone] = useState({ newid: "", name: "", target: guest.node, full: true });
  const [migrate, setMigrate] = useState({ target: nodes.find((n) => n !== guest.node) ?? "", online: true });
  const [showConfig, setShowConfig] = useState(false);
  const label = `${guest.type === "qemu" ? "VM" : "CT"} ${guest.vmid}${guest.name ? ` ${guest.name}` : ""}`;

  useEffect(() => {
    if (!manage) return;
    void api<{ vmid: number }>(`/proxmox/${clusterKey}/nextid`).then((r) => {
      if (r.ok) setClone((c) => ({ ...c, newid: c.newid || String(r.data.vmid), name: c.name || `${guest.name ?? "guest"}-clone` }));
    });
  }, [clusterKey, manage, guest.name]);

  async function run(path: string, init: RequestInit, what: string, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    setError(null);
    const r = await api<Started>(path, init);
    if (!r.ok) return setError(message(r.code));
    onStarted(r.data, `${label}: ${what}`);
    setTimeout(detail.reload, 1500);
  }

  const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } });
  const snapshots = (detail.data?.snapshots ?? []).filter((s) => !s.current);

  return (
    <Modal title={label} onClose={onClose} wide>
      <div className="max-h-[75vh] space-y-4 overflow-y-auto pr-1">
        {error && <Alert>{error}</Alert>}
        {detail.error && <Alert>{detail.error}</Alert>}
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge status={guest.status} />
          <span className="font-mono text-fg-muted">
            on {guest.node}
            {guest.uptime ? ` · up ${formatDuration(guest.uptime)}` : ""}
            {guest.hastate ? ` · HA ${guest.hastate}` : ""}
            {guest.lock ? ` · locked (${guest.lock})` : ""}
          </span>
          {can("proxmox.console") && guest.status === "running" && (
            <Button className="ml-auto" onClick={onConsole} disabled={guest.type === "qemu" && detail.data !== null && !detail.data.serial} title={guest.type === "qemu" && detail.data && !detail.data.serial ? "Add a serial port (serial0: socket) to the VM to use the console" : undefined}>
              <SquareTerminal className="h-3 w-3" aria-hidden /> Console
            </Button>
          )}
        </div>

        <section>
          <h3 className="mb-1 text-xs font-semibold uppercase tracking-wider text-fg-muted">Snapshots</h3>
          {detail.data && snapshots.length === 0 && <p className="text-xs text-fg-muted">No snapshots.</p>}
          <table className="w-full text-xs">
            <tbody className="divide-y divide-line">
              {snapshots.map((s) => (
                <tr key={s.name}>
                  <td className="py-1 pr-2 font-semibold">{s.name}</td>
                  <td className="py-1 pr-2 text-fg-secondary">{s.description}</td>
                  <td className="whitespace-nowrap py-1 pr-2 font-mono text-fg-muted">{when(s.time)}</td>
                  <td className="py-1 pr-2 text-fg-muted">{s.vmstate ? "with RAM" : ""}</td>
                  <td className="py-1 text-right">
                    {manage && (
                      <span className="inline-flex gap-0.5">
                        <IconButton title={`Roll back to ${s.name}`} onClick={() => void run(`${base}/snapshots/${s.name}/rollback`, { method: "POST" }, `rollback to ${s.name}`, `Roll back ${label} to “${s.name}”? Changes since then are lost.`)}>
                          <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                        </IconButton>
                        <IconButton danger title={`Delete ${s.name}`} onClick={() => void run(`${base}/snapshots/${s.name}`, { method: "DELETE" }, `delete snapshot ${s.name}`, `Delete the snapshot “${s.name}”?`)}>
                          <Trash2 className="h-3.5 w-3.5" aria-hidden />
                        </IconButton>
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {manage && (
            <form
              className="mt-2 flex flex-wrap items-end gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                void run(`${base}/snapshots`, json({ name: snap.name, description: snap.description || null, vmstate: snap.vmstate }), `snapshot ${snap.name}`).then(() => setSnap({ name: "", description: "", vmstate: false }));
              }}
            >
              <div className="w-40">
                <TextInput value={snap.name} onChange={(e) => setSnap({ ...snap, name: e.target.value })} placeholder="name" aria-label="Snapshot name" pattern="[A-Za-z][A-Za-z0-9_\-]{1,39}" required />
              </div>
              <div className="min-w-48 flex-1">
                <TextInput value={snap.description} onChange={(e) => setSnap({ ...snap, description: e.target.value })} placeholder="description (optional)" aria-label="Snapshot description" />
              </div>
              {guest.type === "qemu" && (
                <label className="flex items-center gap-1 text-xs">
                  <input type="checkbox" checked={snap.vmstate} onChange={(e) => setSnap({ ...snap, vmstate: e.target.checked })} /> include RAM
                </label>
              )}
              <Button type="submit">
                <Camera className="h-3 w-3" aria-hidden /> Take snapshot
              </Button>
            </form>
          )}
        </section>

        {manage && !guest.template && (
          <div className="grid gap-4 md:grid-cols-2">
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wider text-fg-muted">Clone</h3>
              <form
                className="space-y-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  void run(`${base}/clone`, json({ newid: Number(clone.newid), name: clone.name || null, target: clone.target || null, full: clone.full }), `clone to ${clone.newid}`);
                }}
              >
                <div className="grid grid-cols-2 gap-2">
                  <Field label="New VMID">
                    <TextInput type="number" min={100} value={clone.newid} onChange={(e) => setClone({ ...clone, newid: e.target.value })} required />
                  </Field>
                  <Field label="Name">
                    <TextInput value={clone.name} onChange={(e) => setClone({ ...clone, name: e.target.value })} />
                  </Field>
                </div>
                <div className="flex items-end gap-2">
                  <Field label="Target node">
                    <Select value={clone.target} onChange={(e) => setClone({ ...clone, target: e.target.value })}>
                      {nodes.map((n) => (
                        <option key={n}>{n}</option>
                      ))}
                    </Select>
                  </Field>
                  <label className="mb-2 flex items-center gap-1 whitespace-nowrap text-xs">
                    <input type="checkbox" checked={clone.full} onChange={(e) => setClone({ ...clone, full: e.target.checked })} /> full clone
                  </label>
                </div>
                <Button type="submit">
                  <Copy className="h-3 w-3" aria-hidden /> Clone
                </Button>
              </form>
            </section>
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wider text-fg-muted">Migrate</h3>
              {nodes.length < 2 ? (
                <p className="text-xs text-fg-muted">A single node: nowhere to migrate.</p>
              ) : (
                <form
                  className="space-y-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void run(`${base}/migrate`, json(migrate), `migrate to ${migrate.target}`, `Migrate ${label} from ${guest.node} to ${migrate.target}?`);
                  }}
                >
                  <Field label="Target node">
                    <Select value={migrate.target} onChange={(e) => setMigrate({ ...migrate, target: e.target.value })}>
                      {nodes
                        .filter((n) => n !== guest.node)
                        .map((n) => (
                          <option key={n}>{n}</option>
                        ))}
                    </Select>
                  </Field>
                  <label className="flex items-center gap-1 text-xs">
                    <input type="checkbox" checked={migrate.online} onChange={(e) => setMigrate({ ...migrate, online: e.target.checked })} />
                    {guest.type === "qemu" ? "online (live migration)" : "restart mode (stop, move, start)"}
                  </label>
                  <Button type="submit">
                    <MoveRight className="h-3 w-3" aria-hidden /> Migrate
                  </Button>
                </form>
              )}
            </section>
          </div>
        )}

        <section>
          <button type="button" className="text-xs font-semibold uppercase tracking-wider text-fg-muted hover:text-fg" onClick={() => setShowConfig((v) => !v)}>
            {showConfig ? "▾" : "▸"} Configuration
          </button>
          {showConfig && detail.data && (
            <dl className="mt-1 grid grid-cols-[10rem_1fr] gap-x-3 gap-y-0.5 font-mono text-[11px]">
              {Object.entries(detail.data.config).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-fg-muted">{k}</dt>
                  <dd className="break-all">{String(v)}</dd>
                </div>
              ))}
            </dl>
          )}
        </section>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- tabs

function GuestsTab({
  data,
  onOpen,
  onPower,
  onConsole,
}: {
  data: Overview;
  onOpen: (g: Guest) => void;
  onPower: (g: Guest, action: string) => void;
  onConsole: (g: Guest) => void;
}) {
  const { can } = useAuth();
  const manage = can("proxmox.manage");
  const [query, setQuery] = useState("");
  const [node, setNode] = useState("");
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "vmid", desc: false });
  const q = query.trim().toLowerCase();
  const rows = data.guests.filter(
    (g) =>
      (!node || g.node === node) &&
      (!kind || g.type === kind) &&
      (!status || g.status === status) &&
      (!q || [String(g.vmid), g.name ?? "", ...g.tags, g.pool ?? ""].some((v) => v.toLowerCase().includes(q))),
  );
  const value: Record<SortKey, (g: Guest) => string | number> = {
    vmid: (g) => g.vmid,
    name: (g) => g.name ?? "",
    node: (g) => g.node,
    status: (g) => g.status,
    cpu: (g) => g.cpu ?? -1,
    mem: (g) => g.mem ?? -1,
    uptime: (g) => g.uptime ?? -1,
  };
  rows.sort((a, b) => {
    const va = value[sort.key](a);
    const vb = value[sort.key](b);
    const c = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
    return sort.desc ? -c : c;
  });
  const header = (key: SortKey | null, label: string, cls = "") => (
    <th className={`whitespace-nowrap px-2 py-2 font-semibold ${cls}`} aria-sort={key && sort.key === key ? (sort.desc ? "descending" : "ascending") : undefined}>
      {key ? (
        <button type="button" className="inline-flex items-center gap-1 hover:text-fg" onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : false }))}>
          {label}
          {sort.key === key ? sort.desc ? <ArrowDown className="h-3 w-3" /> : <ArrowUp className="h-3 w-3" /> : <ArrowUpDown className="h-3 w-3 opacity-40" />}
        </button>
      ) : (
        label
      )}
    </th>
  );
  const sel = "rounded border border-line bg-canvas px-2 py-1.5 text-xs";
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-full bg-elevated px-2 py-0.5 font-mono text-xs">
          {rows.length}
          {rows.length !== data.guests.length ? ` / ${data.guests.length}` : ""}
        </span>
        <div className="ml-auto flex flex-wrap gap-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-muted" aria-hidden />
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search VMID, name, tag, pool…" aria-label="Search guests" className="w-56 rounded border border-line bg-canvas py-1.5 pl-7 pr-2 text-xs outline-none focus:border-primary" />
          </div>
          <select className={sel} value={node} onChange={(e) => setNode(e.target.value)} aria-label="Filter by node">
            <option value="">All nodes</option>
            {data.nodes.map((n) => (
              <option key={n.node}>{n.node}</option>
            ))}
          </select>
          <select className={sel} value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Filter by type">
            <option value="">VMs and containers</option>
            <option value="qemu">VMs</option>
            <option value="lxc">Containers</option>
          </select>
          <select className={sel} value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by status">
            <option value="">All states</option>
            {[...new Set(data.guests.map((g) => g.status))].sort().map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </div>
      </div>
      <div className="overflow-x-auto rounded-lg border border-line bg-surface">
        <table className="w-full text-xs">
          <thead className="bg-elevated text-left text-fg-secondary">
            <tr>
              {header("vmid", "ID")}
              {header("name", "Name")}
              {header(null, "Type")}
              {header("node", "Node")}
              {header("status", "State")}
              {header("cpu", "CPU", "text-right")}
              {header("mem", "Memory", "text-right")}
              {header(null, "Disk", "text-right")}
              {header("uptime", "Uptime")}
              {header(null, "Tags")}
              {header(null, "Actions", "text-right")}
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.length === 0 && (
              <tr>
                <td colSpan={11} className="px-2 py-4 text-center text-fg-muted">
                  No guest matches the filters.
                </td>
              </tr>
            )}
            {rows.map((g) => {
              const running = g.status === "running";
              const cpu = g.cpu !== null && running ? g.cpu * 100 : null;
              return (
                <tr key={g.vmid} className={`hover:bg-elevated/50 ${g.template ? "opacity-60" : ""}`}>
                  <td className="px-2 py-2 font-mono">{g.vmid}</td>
                  <td className="px-2 py-2">
                    <button type="button" className="font-semibold hover:text-primary" onClick={() => onOpen(g)}>
                      {g.name ?? "–"}
                    </button>
                    {g.lock && <span className="ml-1 text-[10px] text-warn">🔒 {g.lock}</span>}
                    {g.hastate && <span className="ml-1 text-[10px] text-fg-muted">HA</span>}
                  </td>
                  <td className="px-2 py-2 text-fg-secondary">{g.template ? "template" : g.type === "qemu" ? "VM" : "CT"}</td>
                  <td className="px-2 py-2 font-mono">{g.node}</td>
                  <td className="px-2 py-2">
                    <Badge status={g.status} />
                  </td>
                  <td className="whitespace-nowrap px-2 py-2 text-right font-mono tabular-nums">
                    {cpu !== null ? `${cpu.toFixed(1)}%` : "–"}
                    <span className="text-fg-muted"> /{g.maxcpu ?? "?"}</span>
                  </td>
                  <td className="whitespace-nowrap px-2 py-2 text-right font-mono tabular-nums">
                    {running && g.mem !== null ? formatBytes(g.mem) : "–"}
                    <span className="text-fg-muted">/{g.maxmem ? formatBytes(g.maxmem) : "?"}</span>
                  </td>
                  <td className="whitespace-nowrap px-2 py-2 text-right font-mono tabular-nums text-fg-secondary">{g.maxdisk ? formatBytes(g.maxdisk) : "–"}</td>
                  <td className="whitespace-nowrap px-2 py-2 text-fg-secondary">{running && g.uptime ? formatDuration(g.uptime) : "–"}</td>
                  <td className="px-2 py-2">
                    <div className="flex flex-wrap gap-1">
                      {g.tags.map((t) => (
                        <span key={t} className="rounded bg-elevated px-1.5 py-0.5 text-[10px]">
                          {t}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="px-2 py-2">
                    <div className="flex justify-end gap-0.5">
                      <IconButton title={`Details of ${g.vmid}`} onClick={() => onOpen(g)}>
                        <Info className="h-3.5 w-3.5" aria-hidden />
                      </IconButton>
                      {running && can("proxmox.console") && (
                        <IconButton title={`Console of ${g.vmid}`} onClick={() => onConsole(g)}>
                          <SquareTerminal className="h-3.5 w-3.5" aria-hidden />
                        </IconButton>
                      )}
                      {manage && !g.template && (
                        <>
                          {g.status === "stopped" && (
                            <IconButton title={`Start ${g.vmid}`} onClick={() => onPower(g, "start")}>
                              <Play className="h-3.5 w-3.5" aria-hidden />
                            </IconButton>
                          )}
                          {(g.status === "paused" || g.status === "suspended") && (
                            <IconButton title={`Resume ${g.vmid}`} onClick={() => onPower(g, "resume")}>
                              <Play className="h-3.5 w-3.5" aria-hidden />
                            </IconButton>
                          )}
                          {running && (
                            <>
                              <IconButton title={`Reboot ${g.vmid}`} onClick={() => onPower(g, "reboot")}>
                                <RotateCw className="h-3.5 w-3.5" aria-hidden />
                              </IconButton>
                              {g.type === "qemu" && (
                                <IconButton title={`Suspend ${g.vmid}`} onClick={() => onPower(g, "suspend")}>
                                  <Pause className="h-3.5 w-3.5" aria-hidden />
                                </IconButton>
                              )}
                              <IconButton title={`Shut down ${g.vmid}`} onClick={() => onPower(g, "shutdown")}>
                                <Power className="h-3.5 w-3.5" aria-hidden />
                              </IconButton>
                              <IconButton danger title={`Stop ${g.vmid} (power off)`} onClick={() => onPower(g, "stop")}>
                                <Square className="h-3.5 w-3.5" aria-hidden />
                              </IconButton>
                            </>
                          )}
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function NodesTab({ data, onShell }: { data: Overview; onShell: (node: string) => void }) {
  const { can } = useAuth();
  const shell = can("proxmox.console") && can("ssh.connect");
  const storage = new Map<string, Overview["storage"]>();
  for (const s of data.storage) {
    const key = s.shared ? s.storage : `${s.node}/${s.storage}`;
    storage.set(key, [...(storage.get(key) ?? []), s]);
  }
  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {data.nodes.map((n) => {
          const guests = data.guests.filter((g) => g.node === n.node && !g.template);
          return (
            <section key={n.node} className="rounded-lg border border-line bg-surface p-4">
              <div className="mb-3 flex items-center gap-2">
                <h2 className="font-semibold">{n.node}</h2>
                <Badge status={n.status} />
                <span className="font-mono text-[11px] text-fg-muted">{n.ip}</span>
                <span className="ml-auto flex gap-1">
                  {n.inventory_id && (
                    <Link href={`/nodes/?id=${n.inventory_id}`} className="rounded p-1 text-fg-muted hover:bg-elevated hover:text-fg" title="Inventory node">
                      <ExternalLink className="h-3.5 w-3.5" aria-hidden />
                    </Link>
                  )}
                  {shell && n.status === "online" && (
                    <IconButton title={`Shell on ${n.node}`} onClick={() => onShell(n.node)}>
                      <SquareTerminal className="h-3.5 w-3.5" aria-hidden />
                    </IconButton>
                  )}
                </span>
              </div>
              <dl className="grid grid-cols-[4.5rem_1fr_auto] items-center gap-x-2 gap-y-1.5 text-xs">
                <dt className="text-fg-muted">CPU</dt>
                <dd>
                  <Bar value={n.cpu !== null ? n.cpu * 100 : null} label="CPU" />
                </dd>
                <dd className="text-right font-mono tabular-nums">
                  {n.cpu !== null ? `${(n.cpu * 100).toFixed(1)}%` : "–"} <span className="text-fg-muted">of {n.maxcpu}</span>
                </dd>
                <dt className="text-fg-muted">Memory</dt>
                <dd>
                  <Bar value={pct(n.mem, n.maxmem)} label="Memory" />
                </dd>
                <dd className="text-right font-mono tabular-nums">
                  {n.mem !== null ? formatBytes(n.mem) : "–"} <span className="text-fg-muted">/ {n.maxmem ? formatBytes(n.maxmem) : "?"}</span>
                </dd>
                <dt className="text-fg-muted">Root disk</dt>
                <dd>
                  <Bar value={pct(n.disk, n.maxdisk)} label="Root disk" />
                </dd>
                <dd className="text-right font-mono tabular-nums">
                  {n.disk !== null ? formatBytes(n.disk) : "–"} <span className="text-fg-muted">/ {n.maxdisk ? formatBytes(n.maxdisk) : "?"}</span>
                </dd>
              </dl>
              <p className="mt-3 font-mono text-[11px] text-fg-muted">
                {guests.filter((g) => g.status === "running").length}/{guests.length} guests running
                {n.uptime ? ` · up ${formatDuration(n.uptime)}` : ""}
              </p>
            </section>
          );
        })}
      </div>
      <Card title="Storage">
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-left text-fg-muted">
              <tr>
                <th className="py-1 pr-3 font-semibold">Storage</th>
                <th className="py-1 pr-3 font-semibold">Nodes</th>
                <th className="py-1 pr-3 font-semibold">Type</th>
                <th className="py-1 pr-3 font-semibold">Content</th>
                <th className="py-1 pr-3 font-semibold">Usage</th>
                <th className="py-1 text-right font-semibold">Used / total</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {[...storage.entries()].map(([key, list]) => {
                const s = list[0];
                const down = list.some((x) => x.status !== "available");
                return (
                  <tr key={key}>
                    <td className="py-1.5 pr-3 font-semibold">
                      {s.storage}
                      {down && <span className="ml-1 text-crit">· {list.find((x) => x.status !== "available")?.status}</span>}
                    </td>
                    <td className="py-1.5 pr-3 font-mono">{s.shared ? `shared (${list.map((x) => x.node).join(", ")})` : s.node}</td>
                    <td className="py-1.5 pr-3 text-fg-secondary">{s.type}</td>
                    <td className="py-1.5 pr-3 text-fg-muted">{s.content.join(", ")}</td>
                    <td className="w-40 py-1.5 pr-3">
                      <Bar value={pct(s.disk, s.maxdisk)} label={`${s.storage} usage`} />
                    </td>
                    <td className="whitespace-nowrap py-1.5 text-right font-mono tabular-nums">
                      {s.disk !== null ? formatBytes(s.disk) : "–"} / {s.maxdisk ? formatBytes(s.maxdisk) : "?"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function BackupsTab({ clusterKey, onLog }: { clusterKey: string; onLog: (t: { node: string; upid: string; label: string }) => void }) {
  const [maxAge, setMaxAge] = useState("");
  const audit = useResource<BackupAudit>(`/proxmox/${clusterKey}/backups${maxAge ? `?max_age_h=${maxAge}` : ""}`);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {audit.data &&
          (["failed", "missing", "stale", "unscheduled", "ok"] as const).map((s) => (
            <span key={s} className={`rounded border px-2 py-0.5 font-semibold ${BACKUP_STYLE[s].className}`}>
              {BACKUP_STYLE[s].label}: {audit.data?.summary[s] ?? 0}
            </span>
          ))}
        <label className="ml-auto flex items-center gap-2 text-fg-muted">
          Maximum age
          <select value={maxAge} onChange={(e) => setMaxAge(e.target.value)} className="rounded border border-line bg-canvas px-2 py-1.5 text-xs text-fg">
            <option value="">from each job’s schedule</option>
            <option value="26">26 hours</option>
            <option value="50">2 days</option>
            <option value="194">8 days</option>
            <option value="744">31 days</option>
          </select>
        </label>
        <Button onClick={audit.reload}>
          <RefreshCw className="h-3 w-3" aria-hidden /> Refresh
        </Button>
      </div>
      {audit.error && <Alert>{audit.error}</Alert>}
      {!audit.data && !audit.error && <p className="text-sm text-fg-muted">Reading backup storages and task logs…</p>}
      {audit.data && audit.data.errors.length > 0 && <Alert kind="info">Partial result: {audit.data.errors.join("; ")}</Alert>}
      {audit.data && (
        <>
          <div className="overflow-x-auto rounded-lg border border-line bg-surface">
            <table className="w-full text-xs">
              <thead className="bg-elevated text-left text-fg-secondary">
                <tr>
                  <th className="px-2 py-2 font-semibold">ID</th>
                  <th className="px-2 py-2 font-semibold">Guest</th>
                  <th className="px-2 py-2 font-semibold">Node</th>
                  <th className="px-2 py-2 font-semibold">Backup</th>
                  <th className="px-2 py-2 font-semibold">Last backup</th>
                  <th className="px-2 py-2 text-right font-semibold">Age / allowed</th>
                  <th className="px-2 py-2 text-right font-semibold">Size</th>
                  <th className="px-2 py-2 font-semibold">Storage</th>
                  <th className="px-2 py-2 text-right font-semibold">Archives</th>
                  <th className="px-2 py-2 font-semibold">Jobs</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {audit.data.guests.map((g) => (
                  <tr key={g.vmid}>
                    <td className="px-2 py-2 font-mono">{g.vmid}</td>
                    <td className="px-2 py-2 font-semibold">
                      {g.name} <span className="font-normal text-fg-muted">{g.type === "qemu" ? "VM" : "CT"}</span>
                    </td>
                    <td className="px-2 py-2 font-mono">{g.node}</td>
                    <td className="px-2 py-2">
                      <span className={`rounded border px-1.5 py-0.5 font-semibold ${BACKUP_STYLE[g.status].className}`} title={g.failed_at ? `vzdump failed at ${when(g.failed_at)}` : undefined}>
                        {BACKUP_STYLE[g.status].label}
                      </span>
                      {g.verified && <span className="ml-1 text-[10px] text-fg-muted">verify {g.verified}</span>}
                    </td>
                    <td className="whitespace-nowrap px-2 py-2 font-mono text-fg-secondary">{when(g.last_backup)}</td>
                    <td className="whitespace-nowrap px-2 py-2 text-right font-mono tabular-nums">
                      {g.age_s !== null ? formatDuration(g.age_s) : "–"} <span className="text-fg-muted">/ {formatDuration(g.max_age_s)}</span>
                    </td>
                    <td className="whitespace-nowrap px-2 py-2 text-right font-mono tabular-nums">{g.size !== null ? formatBytes(g.size) : "–"}</td>
                    <td className="px-2 py-2 font-mono">{g.storage ?? "–"}</td>
                    <td className="px-2 py-2 text-right font-mono">{g.archives}</td>
                    <td className="px-2 py-2 font-mono text-[10px] text-fg-muted">{g.jobs.join(", ") || "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="grid gap-3 lg:grid-cols-2">
            <Card title="Backup jobs">
              {audit.data.jobs.length === 0 ? (
                <p className="text-xs text-fg-muted">No vzdump jobs: Datacenter → Backup in Proxmox.</p>
              ) : (
                <table className="w-full text-xs">
                  <tbody className="divide-y divide-line">
                    {audit.data.jobs.map((j) => (
                      <tr key={j.id} className={j.enabled ? "" : "opacity-50"}>
                        <td className="py-1.5 pr-2 font-mono text-[10px]">{j.id}</td>
                        <td className="py-1.5 pr-2 font-mono">{j.schedule}</td>
                        <td className="py-1.5 pr-2">{j.selection}</td>
                        <td className="py-1.5 pr-2 font-mono">{j.storage}</td>
                        <td className="py-1.5 text-fg-muted">
                          {j.mode}
                          {!j.enabled && " · disabled"}
                          {j.comment ? ` · ${j.comment}` : ""}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Card>
            <Card title="Failed backup tasks">
              {audit.data.failed_tasks.length === 0 ? (
                <p className="text-xs text-fg-muted">No failed vzdump task in the recent history.</p>
              ) : (
                <table className="w-full text-xs">
                  <tbody className="divide-y divide-line">
                    {audit.data.failed_tasks.map((t) => (
                      <tr key={t.upid}>
                        <td className="whitespace-nowrap py-1.5 pr-2 font-mono">{when(t.starttime)}</td>
                        <td className="py-1.5 pr-2 font-mono">{t.node}</td>
                        <td className="py-1.5 pr-2">{t.id ? `VM ${t.id}` : "job"}</td>
                        <td className="py-1.5 pr-2 text-crit">{t.status}</td>
                        <td className="py-1.5 text-right">
                          <button type="button" className="text-fg-muted hover:text-fg" onClick={() => onLog({ node: t.node, upid: t.upid, label: `Backup ${t.id || "job"} · ${when(t.starttime)}` })}>
                            log
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Card>
          </div>
          <p className="text-[10px] text-fg-muted">
            Archives read from {audit.data.storages.join(", ") || "no backup storage"}. A guest is “Too old” past its most frequent job’s interval plus 6 hours (one day without a job); “No job” has a backup but no enabled job includes it.
          </p>
        </>
      )}
    </div>
  );
}

function TasksTab({ clusterKey, onLog }: { clusterKey: string; onLog: (t: { node: string; upid: string; label: string }) => void }) {
  const tasks = useResource<Task[]>(`/proxmox/${clusterKey}/tasks`);
  return (
    <div className="space-y-2">
      <div className="flex justify-end">
        <Button onClick={tasks.reload}>
          <RefreshCw className="h-3 w-3" aria-hidden /> Refresh
        </Button>
      </div>
      {tasks.error && <Alert>{tasks.error}</Alert>}
      <div className="overflow-x-auto rounded-lg border border-line bg-surface">
        <table className="w-full text-xs">
          <thead className="bg-elevated text-left text-fg-secondary">
            <tr>
              <th className="px-2 py-2 font-semibold">Started</th>
              <th className="px-2 py-2 font-semibold">Node</th>
              <th className="px-2 py-2 font-semibold">Task</th>
              <th className="px-2 py-2 font-semibold">User</th>
              <th className="px-2 py-2 text-right font-semibold">Duration</th>
              <th className="px-2 py-2 font-semibold">Status</th>
              <th className="px-2 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {(tasks.data ?? []).map((t) => (
              <tr key={t.upid}>
                <td className="whitespace-nowrap px-2 py-1.5 font-mono">{when(t.starttime)}</td>
                <td className="px-2 py-1.5 font-mono">{t.node}</td>
                <td className="px-2 py-1.5">{taskLabel(t.type, t.id)}</td>
                <td className="px-2 py-1.5 font-mono text-fg-muted">{t.user}</td>
                <td className="whitespace-nowrap px-2 py-1.5 text-right font-mono tabular-nums">{t.endtime ? formatDuration(Math.max(60, t.endtime - t.starttime)) : "running"}</td>
                <td className={`px-2 py-1.5 ${!t.status ? "text-fg-muted" : t.status === "OK" ? "text-ok" : t.status.startsWith("WARNINGS") ? "text-warn" : "text-crit"}`}>{t.status ?? "running"}</td>
                <td className="px-2 py-1.5 text-right">
                  <button type="button" className="text-fg-muted hover:text-fg" onClick={() => onLog({ node: t.node, upid: t.upid, label: taskLabel(t.type, t.id) })}>
                    log
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- page

const POWER_CONFIRM: Record<string, string> = {
  stop: "Power off %s immediately (like pulling the plug)?",
  shutdown: "Shut down %s?",
  reboot: "Reboot %s?",
  suspend: "Suspend %s?",
};

function ClusterView({ cluster }: { cluster: Cluster }) {
  const key = cluster.key;
  const overview = useResource<Overview>(`/proxmox/${key}/overview`);
  const [tab, setTab] = useState<Tab>("guests");
  const [tracked, setTracked] = useState<Tracked[]>([]);
  const [guest, setGuest] = useState<Guest | null>(null);
  const [console_, setConsole] = useState<ConsoleTarget | null>(null);
  const [log, setLog] = useState<{ node: string; upid: string; label: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = overview.reload;

  useEffect(() => {
    const timer = setInterval(reload, 20_000);
    return () => clearInterval(timer);
  }, [reload]);

  const started = useCallback((s: Started, label: string) => setTracked((t) => [{ ...s, label }, ...t].slice(0, 8)), []);
  const onDone = useCallback(() => reload(), [reload]);

  async function power(g: Guest, action: string) {
    const name = `${g.type === "qemu" ? "VM" : "CT"} ${g.vmid}${g.name ? ` (${g.name})` : ""}`;
    if (POWER_CONFIRM[action] && !window.confirm(POWER_CONFIRM[action].replace("%s", name))) return;
    setError(null);
    const r = await api<Started>(`/proxmox/${key}/guests/${g.node}/${g.type}/${g.vmid}/status/${action}`, { method: "POST" });
    if (!r.ok) return setError(message(r.code));
    started(r.data, `${name}: ${action}`);
  }

  const data = overview.data;
  const tabs: [Tab, string][] = [
    ["guests", `VMs & containers${data ? ` (${data.guests.length})` : ""}`],
    ["nodes", "Nodes & storage"],
    ["backups", "Backups"],
    ["tasks", "Tasks"],
  ];
  return (
    <div className="space-y-3">
      {data && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-line bg-surface px-4 py-2 text-xs">
          <span className="font-semibold">{data.cluster.standalone ? `Node ${data.cluster.name}` : `Cluster ${data.cluster.name}`}</span>
          {!data.cluster.standalone && (
            <span className={`rounded border px-1.5 py-0.5 font-semibold ${data.cluster.quorate ? "border-ok/40 bg-ok/15 text-ok" : "border-crit/40 bg-crit/15 text-crit"}`}>{data.cluster.quorate ? "quorate" : "NO QUORUM"}</span>
          )}
          <span className="font-mono text-fg-muted">
            {data.cluster.nodes_online}/{data.cluster.nodes_total} nodes online · {data.guests.filter((g) => g.status === "running").length}/{data.guests.filter((g) => !g.template).length} guests running · PVE {data.cluster.version} · via {cluster.endpoints.map((e) => e.node_name).join(", ")}
          </span>
          <Button className="ml-auto" onClick={reload}>
            <RefreshCw className="h-3 w-3" aria-hidden /> Refresh
          </Button>
        </div>
      )}
      {overview.error && <Alert>{overview.error}</Alert>}
      {error && <Alert>{error}</Alert>}
      <TaskTray clusterKey={key} tasks={tracked} onDone={onDone} onOpen={(t) => setLog(t)} onDismiss={(upid) => setTracked((t) => t.filter((x) => x.upid !== upid))} />
      <div className="flex flex-wrap gap-1 border-b border-line" role="tablist">
        {tabs.map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            className={`-mb-px border-b-2 px-3 py-1.5 text-xs font-semibold ${tab === id ? "border-primary text-primary" : "border-transparent text-fg-muted hover:text-fg"}`}
          >
            {label}
          </button>
        ))}
      </div>
      {!data && !overview.error && <p className="text-sm text-fg-muted">Loading…</p>}
      {data && tab === "guests" && (
        <GuestsTab data={data} onOpen={setGuest} onPower={(g, a) => void power(g, a)} onConsole={(g) => setConsole({ pvenode: g.node, kind: g.type, vmid: g.vmid, title: `${g.vmid} ${g.name ?? ""}` })} />
      )}
      {data && tab === "nodes" && <NodesTab data={data} onShell={(n) => setConsole({ pvenode: n, kind: "node", title: `shell ${n}` })} />}
      {tab === "backups" && <BackupsTab clusterKey={key} onLog={setLog} />}
      {tab === "tasks" && <TasksTab clusterKey={key} onLog={setLog} />}
      {guest && data && (
        <GuestDialog
          clusterKey={key}
          guest={data.guests.find((g) => g.vmid === guest.vmid) ?? guest}
          nodes={data.nodes.filter((n) => n.status === "online").map((n) => n.node)}
          onClose={() => setGuest(null)}
          onStarted={started}
          onConsole={() => {
            setConsole({ pvenode: guest.node, kind: guest.type, vmid: guest.vmid, title: `${guest.vmid} ${guest.name ?? ""}` });
            setGuest(null);
          }}
        />
      )}
      {console_ && <ConsoleDialog clusterKey={key} target={console_} onClose={() => setConsole(null)} />}
      {log && <TaskLog clusterKey={key} task={log} onClose={() => setLog(null)} />}
    </div>
  );
}

export default function ProxmoxPage() {
  const clusters = useResource<Cluster[]>("/proxmox/clusters");
  const [selected, setSelected] = useState<string | null>(null);
  const list = clusters.data ?? [];
  const current = list.find((c) => c.key === selected) ?? list.find((c) => !c.error) ?? list[0];
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Proxmox VE</h1>
      {clusters.error && <Alert>{clusters.error}</Alert>}
      {clusters.data && list.length === 0 && (
        <p className="text-sm text-fg-muted">
          No Proxmox hosts. Give a PVE node the Proxmox capability in the inventory, with a Proxmox credential: user and password (user@realm, without two-factor) or an API token (user@realm!token).
        </p>
      )}
      {list.length > 1 && (
        <div className="flex flex-wrap gap-1 border-b border-line" role="tablist">
          {list.map((c) => (
            <button
              key={c.key}
              role="tab"
              aria-selected={current?.key === c.key}
              onClick={() => setSelected(c.key)}
              className={`-mb-px flex items-center gap-1.5 border-b-2 px-3 py-1.5 text-xs font-semibold ${current?.key === c.key ? "border-primary text-primary" : "border-transparent text-fg-muted hover:text-fg"}`}
            >
              <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: c.error ? "var(--color-crit)" : c.quorate === false ? "var(--color-warn)" : "var(--color-ok)" }} />
              {c.name}
            </button>
          ))}
        </div>
      )}
      {current?.error && <Alert>{`${current.name}: ${message(current.error)}`}</Alert>}
      {current && !current.error && <ClusterView key={current.key} cluster={current} />}
    </div>
  );
}
