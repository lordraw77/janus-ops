"use client";

import {
  Activity,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  CloudDownload,
  SearchCheck,
  ExternalLink,
  FileText,
  Layers,
  Pause,
  Play,
  RefreshCw,
  RotateCw,
  Search,
  Square,
  SquareTerminal,
  Trash2,
} from "lucide-react";
import dynamic from "next/dynamic";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { SortTh } from "@/components/SortTh";
import type { TerminalStatus } from "@/components/TerminalView";
import { Alert, Button, Loading, Modal } from "@/components/ui";
import { formatBytes } from "@/lib/agents";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { InventoryNode } from "@/lib/inventory";
import {
  DiskSection,
  ImagesSection,
  NetworksSection,
  StacksSection,
  VolumesSection,
} from "./Resources";
import { type Getters, useSortedRows } from "@/lib/sort";

// xterm.js touches `window`: load it in the browser only
const TerminalView = dynamic(
  () => import("@/components/TerminalView").then((m) => m.TerminalView),
  { ssr: false },
);

type Host = {
  node_id: string;
  node_name: string;
  environment: string;
  via: "agent" | "ssh";
  address: string;
  agent_online: boolean;
};
type Container = {
  id: string;
  name: string;
  image: string;
  state: string;
  status: string;
  health: "healthy" | "unhealthy" | "starting" | null;
  created: number;
  ports: string[];
  published: { public: number; private: number }[];
  ips: string[];
  project: string | null;
  service: string | null;
};
type Stack = {
  name: string;
  working_dir: string | null;
  services: string[];
  containers: number;
  running: number;
};
type Info = {
  version: string;
  os: string;
  cpus: number;
  memory: number;
  containers: number;
  running: number;
  images: number;
};
type Stats = {
  cpu_pct: number;
  mem_bytes: number | null;
  mem_limit: number | null;
  mem_pct: number | null;
  net_rx_bytes: number;
  net_tx_bytes: number;
  blk_read_bytes: number;
  blk_write_bytes: number;
  pids: number | null;
};

function LogsDialog({
  host,
  c,
  onClose,
}: {
  host: Host;
  c: Container;
  onClose: () => void;
}) {
  const [lines, setLines] = useState<{ stream: string; data: string }[]>([]);
  const [status, setStatus] = useState("connecting");
  const box = useRef<HTMLPreElement>(null);

  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const params = new URLSearchParams({
      node_id: host.node_id,
      container: c.name,
      tail: "300",
    });
    const ws = new WebSocket(
      `${proto}//${window.location.host}/api/v1/containers/logs/ws?${params}`,
    );
    ws.onopen = () => setStatus("following");
    ws.onmessage = (e: MessageEvent<string>) => {
      const msg = JSON.parse(e.data);
      if (msg.type === "log")
        setLines((prev) => [
          ...prev.slice(-4000),
          { stream: msg.stream, data: msg.data },
        ]);
      else if (msg.type === "error") setStatus(message(msg.code));
      else if (msg.type === "end") setStatus("container stopped");
    };
    ws.onclose = () =>
      setStatus((s) => (s === "following" ? "disconnected" : s));
    return () => ws.close();
  }, [host.node_id, c.name]);

  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines]);

  return (
    <Modal title={`Logs · ${c.name}`} onClose={onClose} wide>
      <div>
        <div className="mb-1 font-mono text-[10px] text-fg-muted">{status}</div>
        <pre
          ref={box}
          className="h-[60vh] overflow-auto rounded bg-canvas p-2 font-mono text-[11px] leading-snug"
        >
          {lines.map((l, i) => (
            <span
              key={i}
              className={
                l.stream === "stderr" ? "text-warn" : "text-fg-secondary"
              }
            >
              {l.data}
            </span>
          ))}
        </pre>
      </div>
    </Modal>
  );
}

function StatsDialog({
  host,
  c,
  onClose,
}: {
  host: Host;
  c: Container;
  onClose: () => void;
}) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      void api<Stats>(`/containers/${host.node_id}/c/${c.name}/stats`).then(
        (r) => {
          if (cancelled) return;
          if (r.ok) setStats(r.data);
          else setError(message(r.code));
        },
      );
    load();
    const t = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [host.node_id, c.name]);
  const rows: [string, string][] = stats
    ? [
        ["CPU", `${stats.cpu_pct.toFixed(1)}%`],
        [
          "Memory",
          stats.mem_bytes === null
            ? "–"
            : `${formatBytes(stats.mem_bytes)}${stats.mem_pct !== null ? ` (${stats.mem_pct.toFixed(1)}%)` : ""}`,
        ],
        [
          "Network in / out",
          `${formatBytes(stats.net_rx_bytes)} / ${formatBytes(stats.net_tx_bytes)}`,
        ],
        [
          "Disk read / write",
          `${formatBytes(stats.blk_read_bytes)} / ${formatBytes(stats.blk_write_bytes)}`,
        ],
        ["Processes", String(stats.pids ?? "–")],
      ]
    : [];
  return (
    <Modal title={`Stats · ${c.name}`} onClose={onClose}>
      {error && <Alert>{error}</Alert>}
      {!stats && !error && <Loading />}
      <dl className="grid grid-cols-[9rem_1fr] gap-y-1 font-mono text-xs">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-fg-muted">{k}</dt>
            <dd className="tabular-nums">{v}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-[10px] text-fg-muted">
        Refreshed every 5 s; network and disk are totals since the container
        started.
      </p>
    </Modal>
  );
}

function ExecDialog({
  host,
  c,
  onClose,
}: {
  host: Host;
  c: Container;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<TerminalStatus>({ state: "connecting" });
  const [cmd, setCmd] = useState<string | null>(null);
  return (
    <Modal title={`Shell · ${c.name}`} onClose={onClose} wide>
      <div>
        {cmd === null ? (
          <div className="flex gap-2">
            {["/bin/sh", "/bin/bash"].map((s) => (
              <Button
                key={s}
                onClick={() => setCmd(s)}
                variant={s === "/bin/sh" ? "primary" : "secondary"}
              >
                {s}
              </Button>
            ))}
            <span className="self-center text-xs text-fg-muted">
              The session is recorded in the audit log.
            </span>
          </div>
        ) : (
          <>
            <div className="mb-1 font-mono text-[10px] text-fg-muted">
              {status.state === "connected"
                ? `connected · ${status.user}`
                : status.state === "error"
                  ? message(status.code)
                  : status.state === "closed"
                    ? `closed (${status.reason})`
                    : "connecting…"}
            </div>
            <div className="h-[60vh] rounded bg-canvas p-1">
              <TerminalView
                nodeId={host.node_id}
                active
                onStatus={setStatus}
                wsPath="/api/v1/containers/exec/ws"
                extraParams={{ container: c.name, cmd }}
              />
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

type Live = Stats & { restarts: number; started_at: string | null };
type SortKey =
  | "name"
  | "image"
  | "state"
  | "health"
  | "uptime"
  | "restarts"
  | "cpu"
  | "mem"
  | "ip"
  | "stack";

const STATE_BADGE: Record<string, string> = {
  running: "border-ok/40 bg-ok/15 text-ok",
  paused: "border-warn/40 bg-warn/15 text-warn",
  restarting: "border-warn/40 bg-warn/15 text-warn",
  dead: "border-crit/40 bg-crit/15 text-crit",
};
const HEALTH_DOT: Record<string, { color: string; label: string }> = {
  healthy: { color: "var(--color-ok)", label: "healthy" },
  unhealthy: { color: "var(--color-crit)", label: "unhealthy" },
  starting: { color: "var(--color-warn)", label: "health check starting" },
};
const LIVE_REFRESH_MS = 15_000;
const ALL_HOSTS = "*";

/** docker-stats style sizes: 26.3M, 12G. */
function short(v: number | null | undefined): string {
  if (v === null || v === undefined) return "–";
  const units = ["B", "K", "M", "G", "T"];
  let i = 0;
  let x = v;
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i++;
  }
  return `${i === 0 || x >= 100 ? x.toFixed(0) : x.toFixed(1).replace(/\.0$/, "")}${units[i]}`;
}

function uptime(c: Container, live: Live | undefined, now: number): string {
  if (c.state !== "running" || !live?.started_at)
    return c.status
      .replace(/^Up /, "")
      .replace(/ \((un)?healthy\)| \(health: starting\)/, "");
  const s = Math.max(0, (now - new Date(live.started_at).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} hours`;
  const d = Math.floor(s / 86400);
  return `${d} ${d === 1 ? "day" : "days"}`;
}

function IconButton({
  title,
  onClick,
  disabled,
  danger,
  children,
}: {
  title: string;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
  children: ReactNode;
}) {
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

type DialogKind = "logs" | "stats" | "exec";

type RowProps = {
  host: Host;
  c: Container;
  l: Live | undefined;
  now: number;
  showHost?: boolean;
  manage: boolean;
  canExec: boolean;
  busy: boolean;
  onAct: (action: string) => void;
  onDialog: (kind: DialogKind) => void;
  onStack: (name: string) => void;
  /** Per-host view only: image check result and trigger. */
  upd?: string;
  onCheck?: () => void;
};

/** One container row; shared by the per-host table and the all-hosts table. */
function ContainerRow({
  host,
  c,
  l,
  now,
  showHost = false,
  manage,
  canExec,
  busy,
  onAct,
  onDialog,
  onStack,
  upd,
  onCheck,
}: RowProps) {
  const running = c.state === "running";
  const health = c.health ? HEALTH_DOT[c.health] : undefined;
  return (
    <tr className="hover:bg-elevated/50">
      {showHost && (
        <td className="whitespace-nowrap px-2 py-2">
          <div className="font-semibold">{host.node_name}</div>
          <div className="font-mono text-[10px] text-fg-muted">
            {host.address}
          </div>
        </td>
      )}
      <td
        className="whitespace-nowrap px-2 py-2 font-semibold"
        title={`${c.id}${c.service ? ` · service ${c.service}` : ""}`}
      >
        {c.name}
      </td>
      <td className="px-2 py-2 text-fg-secondary" title={c.image}>
        <div className="flex max-w-52 items-center gap-1.5">
          <span className="min-w-0 truncate">{c.image}</span>
          {upd === "update_available" && (
            <span className="shrink-0 rounded border border-warn/40 bg-warn/15 px-1 py-0.5 font-semibold text-warn">
              update
            </span>
          )}
          {upd === "up_to_date" && (
            <span className="shrink-0 text-ok" title="Image is up to date">
              ✓
            </span>
          )}
          {upd?.startsWith("error") && (
            <span className="shrink-0 text-danger" title={upd}>
              ?
            </span>
          )}
        </div>
      </td>
      <td className="px-2 py-2">
        <span
          className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-semibold ${STATE_BADGE[c.state] ?? "border-line bg-elevated text-fg-muted"}`}
        >
          {running ? (
            <Play className="h-3 w-3" aria-hidden />
          ) : c.state === "paused" ? (
            <Pause className="h-3 w-3" aria-hidden />
          ) : (
            <Square className="h-3 w-3" aria-hidden />
          )}
          {c.state}
        </span>
      </td>
      <td className="px-2 py-2 text-center">
        {health ? (
          <span
            className="inline-block h-2.5 w-2.5 rounded-full"
            style={{ background: health.color }}
            title={health.label}
            aria-label={health.label}
          />
        ) : (
          <span className="text-fg-muted" title="No health check">
            –
          </span>
        )}
      </td>
      <td className="whitespace-nowrap px-2 py-2 text-fg-secondary">
        {uptime(c, l, now)}
      </td>
      <td className="px-2 py-2 text-right font-mono tabular-nums text-fg-secondary">
        {l ? (l.restarts ? l.restarts : "–") : "–"}
      </td>
      <td className="px-2 py-2 text-right font-mono tabular-nums">
        {l ? `${l.cpu_pct.toFixed(1)}%` : running ? "…" : "–"}
      </td>
      <td className="whitespace-nowrap px-2 py-2 text-right font-mono tabular-nums">
        {l && l.mem_bytes !== null ? (
          <>
            {short(l.mem_bytes)}
            <span className="text-fg-muted">/{short(l.mem_limit)}</span>
          </>
        ) : (
          "–"
        )}
      </td>
      <td className="whitespace-nowrap px-2 py-2 text-right font-mono tabular-nums">
        {l ? (
          <span title="Received / sent since the container started">
            <span className="text-primary">↓</span>
            {short(l.net_rx_bytes)} <span className="text-warn">↑</span>
            {short(l.net_tx_bytes)}
          </span>
        ) : (
          "–"
        )}
      </td>
      <td className="whitespace-nowrap px-2 py-2 text-right font-mono tabular-nums">
        {l ? (
          <span title="Read / written since the container started">
            <span className="text-fg-muted">r</span>
            {short(l.blk_read_bytes)} <span className="text-fg-muted">w</span>
            {short(l.blk_write_bytes)}
          </span>
        ) : (
          "–"
        )}
      </td>
      <td className="whitespace-nowrap px-2 py-2 font-mono">
        {c.ips.join(", ") || "–"}
      </td>
      <td className="px-2 py-2">
        <div className="flex flex-wrap gap-1">
          {c.published.length === 0 && <span className="text-fg-muted">–</span>}
          {c.published.map((p) => (
            <a
              key={`${p.public}:${p.private}`}
              href={`http://${host.address}:${p.public}`}
              target="_blank"
              rel="noreferrer noopener"
              className="inline-flex items-center gap-0.5 rounded bg-elevated px-1.5 py-0.5 font-mono hover:text-primary"
              title={`Open http://${host.address}:${p.public}`}
            >
              {p.public}:{p.private}
              <ExternalLink className="h-2.5 w-2.5" aria-hidden />
            </a>
          ))}
        </div>
      </td>
      <td className="px-2 py-2">
        {c.project ? (
          <button
            type="button"
            onClick={() => onStack(c.project as string)}
            className="whitespace-nowrap rounded border border-line px-1.5 py-0.5 font-semibold hover:border-primary"
            title={`Show only the stack ${c.project}`}
          >
            {c.project}
          </button>
        ) : (
          <span className="text-fg-muted">–</span>
        )}
      </td>
      <td className="sticky right-0 bg-surface px-2 py-2 shadow-[-8px_0_8px_-8px_rgba(0,0,0,0.5)]">
        <div className="flex justify-end gap-0.5">
          <IconButton
            title={`Logs of ${c.name}`}
            onClick={() => onDialog("logs")}
          >
            <FileText className="h-3.5 w-3.5" aria-hidden />
          </IconButton>
          {running && canExec && (
            <IconButton
              title={`Shell in ${c.name}`}
              onClick={() => onDialog("exec")}
            >
              <SquareTerminal className="h-3.5 w-3.5" aria-hidden />
            </IconButton>
          )}
          {running && (
            <IconButton
              title={`Stats of ${c.name}`}
              onClick={() => onDialog("stats")}
            >
              <Activity className="h-3.5 w-3.5" aria-hidden />
            </IconButton>
          )}
          {manage && onCheck && (
            <IconButton
              title={`Check for a newer image of ${c.name}`}
              onClick={onCheck}
              disabled={busy}
            >
              <SearchCheck className="h-3.5 w-3.5" aria-hidden />
            </IconButton>
          )}
          {manage && (
            <IconButton
              title={`Update ${c.name} (pull image and recreate)`}
              onClick={() => onAct("update")}
              disabled={busy}
            >
              <CloudDownload className="h-3.5 w-3.5" aria-hidden />
            </IconButton>
          )}
          {manage && running && (
            <>
              <IconButton
                title={`Restart ${c.name}`}
                onClick={() => onAct("restart")}
                disabled={busy}
              >
                <RotateCw className="h-3.5 w-3.5" aria-hidden />
              </IconButton>
              <IconButton
                title={`Pause ${c.name}`}
                onClick={() => onAct("pause")}
                disabled={busy}
              >
                <Pause className="h-3.5 w-3.5" aria-hidden />
              </IconButton>
              <IconButton
                title={`Stop ${c.name}`}
                onClick={() => onAct("stop")}
                disabled={busy}
              >
                <Square className="h-3.5 w-3.5" aria-hidden />
              </IconButton>
            </>
          )}
          {manage && c.state === "paused" && (
            <IconButton
              title={`Resume ${c.name}`}
              onClick={() => onAct("unpause")}
              disabled={busy}
            >
              <Play className="h-3.5 w-3.5" aria-hidden />
            </IconButton>
          )}
          {manage && !running && c.state !== "paused" && (
            <IconButton
              title={`Start ${c.name}`}
              onClick={() => onAct("start")}
              disabled={busy}
            >
              <Play className="h-3.5 w-3.5" aria-hidden />
            </IconButton>
          )}
          {manage && !running && (
            <IconButton
              title={`Remove ${c.name}`}
              onClick={() => onAct("remove")}
              disabled={busy}
              danger
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden />
            </IconButton>
          )}
        </div>
      </td>
    </tr>
  );
}

function HostView({ host }: { host: Host }) {
  const { can } = useAuth();
  const manage = can("containers.manage");
  const containers = useResource<Container[]>(`/containers/${host.node_id}`);
  const stacks = useResource<Stack[]>(`/containers/${host.node_id}/stacks`);
  const info = useResource<Info>(`/containers/${host.node_id}/info`);
  const [live, setLive] = useState<Record<string, Live>>({});
  const [liveTick, setLiveTick] = useState(0);
  const [dialog, setDialog] = useState<{
    kind: "logs" | "stats" | "exec";
    c: Container;
  } | null>(null);
  const [notice, setNotice] = useState<{
    kind: "ok" | "error";
    text: string;
  } | null>(null);
  const [updates, setUpdates] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [stackFilter, setStackFilter] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({
    key: "name",
    desc: false,
  });
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      const r = await api<Record<string, Live>>(
        `/containers/${host.node_id}/live`,
      );
      if (cancelled) return;
      if (r.ok) {
        setLive(r.data);
        setNow(Date.now());
      }
      timer = setTimeout(() => void load(), LIVE_REFRESH_MS);
    };
    void load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [host.node_id, liveTick]);

  const reload = () => {
    containers.reload();
    stacks.reload();
    info.reload();
    setLiveTick((t) => t + 1);
  };

  async function act(c: Container, action: string) {
    if (
      action === "remove" &&
      !window.confirm(`Remove the container ${c.name}?`)
    )
      return;
    if (
      (action === "stop" || action === "kill") &&
      !window.confirm(`${action} ${c.name}?`)
    )
      return;
    if (
      action === "update" &&
      !window.confirm(`Pull the image and recreate ${c.name}?`)
    )
      return;
    setBusy(c.name);
    const r =
      action === "remove"
        ? await api(`/containers/${host.node_id}/c/${c.name}`, {
            method: "DELETE",
          })
        : await api<{ result?: string }>(
            `/containers/${host.node_id}/c/${c.name}/${action}`,
            { method: "POST" },
          );
    setBusy(null);
    if (action === "update")
      setUpdates((u) => {
        const { [c.name]: _, ...rest } = u;
        return rest;
      });
    setNotice(
      r.ok
        ? {
            kind: "ok",
            text: `${c.name}: ${
              action === "update"
                ? ((r.data as { result?: string } | undefined)?.result ===
                  "up_to_date"
                    ? "already up to date"
                    : "updated")
                : `${action} done`
            }.`,
          }
        : { kind: "error", text: message(r.code) },
    );
    reload();
  }

  async function check(c: Container) {
    setBusy(c.name);
    const r = await api<{ result: string }>(
      `/containers/${host.node_id}/c/${c.name}/check`,
    );
    setBusy(null);
    if (!r.ok) {
      setUpdates((u) => ({ ...u, [c.name]: `error: ${message(r.code)}` }));
      setNotice({ kind: "error", text: `${c.name}: ${message(r.code)}` });
      return;
    }
    setUpdates((u) => ({ ...u, [c.name]: r.data.result }));
    setNotice({
      kind: "ok",
      text: `${c.name}: ${r.data.result === "update_available" ? "a newer image is available" : "image is up to date"}.`,
    });
  }

  async function checkAll() {
    setBusy("*");
    const r = await api<Record<string, string>>(
      `/containers/${host.node_id}/check`,
    );
    setBusy(null);
    if (!r.ok) {
      setNotice({ kind: "error", text: message(r.code) });
      return;
    }
    setUpdates(r.data);
    const vals = Object.values(r.data);
    const n = vals.filter((v) => v === "update_available").length;
    const e = vals.filter((v) => v.startsWith("error")).length;
    setNotice({
      kind: "ok",
      text: `Check all: ${n} update(s) available, ${vals.length - n - e} up to date${e ? `, ${e} not checkable` : ""}.`,
    });
  }

  async function updateAll() {
    const targets = (containers.data ?? []).filter(
      (c) => !stackFilter || stackFilter === "~" ? true : c.project === stackFilter,
    );
    if (
      !window.confirm(
        `Pull images and recreate ${targets.length} container(s) on ${host.node_name}?`,
      )
    )
      return;
    setBusy("*");
    const done: string[] = [];
    const failed: string[] = [];
    for (const c of targets) {
      const r = await api<{ result: string }>(
        `/containers/${host.node_id}/c/${c.name}/update`,
        { method: "POST" },
      );
      if (!r.ok) failed.push(`${c.name}: ${message(r.code)}`);
      else if (r.data.result === "updated") done.push(c.name);
    }
    setBusy(null);
    setUpdates({});
    setNotice(
      failed.length
        ? {
            kind: "error",
            text: `Updated ${done.length}; failed: ${failed.join("; ")}`,
          }
        : {
            kind: "ok",
            text: `Update all: ${done.length} updated, ${targets.length - done.length} already up to date.`,
          },
    );
    reload();
  }

  async function stackAct(name: string, action: string) {
    if (
      !window.confirm(
        action === "update"
          ? `Pull images and recreate every container of the stack ${name}?`
          : `${action} every container of the stack ${name}?`,
      )
    )
      return;
    setBusy(name);
    const r = await api<{ results: Record<string, string> }>(
      `/containers/${host.node_id}/stacks/${name}/${action}`,
      { method: "POST" },
    );
    setBusy(null);
    if (!r.ok) setNotice({ kind: "error", text: message(r.code) });
    else {
      const failed = Object.entries(r.data.results).filter(
        ([, v]) => v !== "ok",
      );
      setNotice(
        failed.length
          ? {
              kind: "error",
              text: `${name}: ${failed.map(([k, v]) => `${k}: ${v}`).join("; ")}`,
            }
          : {
              kind: "ok",
              text:
                action === "update"
                  ? `${name}: ${Object.values(r.data.results).filter((v) => v === "updated").length} updated, rest up to date.`
                  : `${name}: ${action} done.`,
            },
      );
    }
    reload();
  }

  const all = containers.data ?? [];
  const states = [...new Set(all.map((c) => c.state))].sort();
  const q = query.trim().toLowerCase();
  const rows = all.filter(
    (c) =>
      (!stateFilter || c.state === stateFilter) &&
      (!stackFilter ||
        (stackFilter === "~" ? !c.project : c.project === stackFilter)) &&
      (!q ||
        [c.name, c.image, c.project ?? "", c.id, ...c.ips].some((v) =>
          v.toLowerCase().includes(q),
        )),
  );
  const startedAt = (c: Container) =>
    live[c.name]?.started_at
      ? new Date(live[c.name].started_at as string).getTime()
      : Infinity;
  const sortValue: Record<SortKey, (c: Container) => string | number> = {
    name: (c) => c.name,
    image: (c) => c.image,
    state: (c) => c.state,
    health: (c) => c.health ?? "~",
    uptime: (c) => -startedAt(c),
    restarts: (c) => live[c.name]?.restarts ?? -1,
    cpu: (c) => live[c.name]?.cpu_pct ?? -1,
    mem: (c) => live[c.name]?.mem_bytes ?? -1,
    ip: (c) =>
      (c.ips[0] ?? "~")
        .split(".")
        .map((p) => p.padStart(3, "0"))
        .join("."),
    stack: (c) => c.project ?? "~",
  };
  rows.sort((a, b) => {
    const va = sortValue[sort.key](a);
    const vb = sortValue[sort.key](b);
    const cmp =
      typeof va === "number" && typeof vb === "number"
        ? va - vb
        : String(va).localeCompare(String(vb));
    return sort.desc ? -cmp : cmp;
  });

  const header = (key: SortKey | null, label: string, cls = "") => (
    <th
      className={`whitespace-nowrap px-2 py-2 font-semibold ${cls}`}
      aria-sort={
        key && sort.key === key
          ? sort.desc
            ? "descending"
            : "ascending"
          : undefined
      }
    >
      {key ? (
        <button
          type="button"
          className="inline-flex items-center gap-1 hover:text-fg"
          onClick={() =>
            setSort((s) => ({ key, desc: s.key === key ? !s.desc : false }))
          }
        >
          {label}
          {sort.key === key ? (
            sort.desc ? (
              <ArrowDown className="h-3 w-3" />
            ) : (
              <ArrowUp className="h-3 w-3" />
            )
          ) : (
            <ArrowUpDown className="h-3 w-3 opacity-40" />
          )}
        </button>
      ) : (
        label
      )}
    </th>
  );
  const stackInfo =
    stackFilter && stackFilter !== "~"
      ? (stacks.data ?? []).find((s) => s.name === stackFilter)
      : undefined;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="rounded-full bg-elevated px-2 py-0.5 font-mono text-xs"
          title="Containers shown"
        >
          {rows.length}
          {rows.length !== all.length ? ` / ${all.length}` : ""}
        </span>
        {info.data && (
          <span className="font-mono text-[11px] text-fg-muted">
            Docker {info.data.version} · {info.data.os} · {info.data.cpus} CPU ·{" "}
            {formatBytes(info.data.memory)} · {info.data.images} images · via{" "}
            {host.via}
          </span>
        )}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-muted"
              aria-hidden
            />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search name, image, stack, IP…"
              aria-label="Search containers"
              className="w-60 rounded border border-line bg-canvas py-1.5 pl-7 pr-2 text-xs outline-none focus:border-primary"
            />
          </div>
          <select
            value={stateFilter}
            onChange={(e) => setStateFilter(e.target.value)}
            aria-label="Filter by state"
            className="rounded border border-line bg-canvas px-2 py-1.5 text-xs"
          >
            <option value="">All states</option>
            {states.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <select
            value={stackFilter}
            onChange={(e) => setStackFilter(e.target.value)}
            aria-label="Filter by stack"
            className="rounded border border-line bg-canvas px-2 py-1.5 text-xs"
          >
            <option value="">All stacks</option>
            {(stacks.data ?? []).map((s) => (
              <option key={s.name} value={s.name}>
                {s.name} ({s.running}/{s.containers})
              </option>
            ))}
            <option value="~">No stack</option>
          </select>
          {manage && (
            <Button onClick={() => void checkAll()} disabled={busy === "*"}>
              <SearchCheck className="h-3 w-3" aria-hidden /> Check all
            </Button>
          )}
          {manage && (
            <Button onClick={() => void updateAll()} disabled={busy === "*"}>
              <CloudDownload className="h-3 w-3" aria-hidden /> Update all
            </Button>
          )}
          <Button onClick={reload}>
            <RefreshCw className="h-3 w-3" aria-hidden /> Refresh
          </Button>
        </div>
      </div>
      {stackInfo && (
        <div className="flex flex-wrap items-center gap-2 rounded border border-line bg-surface px-3 py-2 text-xs">
          <Layers className="h-3.5 w-3.5 text-fg-muted" aria-hidden />
          <span className="font-semibold">{stackInfo.name}</span>
          <span className="font-mono text-fg-muted">
            {stackInfo.running}/{stackInfo.containers} running
            {stackInfo.working_dir ? ` · ${stackInfo.working_dir}` : ""}
          </span>
          {manage && (
            <span className="ml-auto flex gap-1">
              {(["update", "start", "restart", "stop"] as const).map((a) => (
                <Button
                  key={a}
                  onClick={() => void stackAct(stackInfo.name, a)}
                  disabled={busy === stackInfo.name}
                >
                  {a} stack
                </Button>
              ))}
            </span>
          )}
        </div>
      )}
      {notice && <Alert kind={notice.kind}>{notice.text}</Alert>}
      {containers.error && (
        <Alert>
          {containers.error}
          {host.via === "agent" &&
            " — the agent must be online and installed with “Allow Docker” (node page → Reinstall)."}
        </Alert>
      )}
      {containers.data && (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full text-xs">
            <thead className="bg-elevated text-left text-fg-secondary">
              <tr>
                {header("name", "Name")}
                {header("image", "Image")}
                {header("state", "State")}
                {header("health", "Health")}
                {header("uptime", "Uptime")}
                {header("restarts", "Restarts", "text-right")}
                {header("cpu", "CPU", "text-right")}
                {header("mem", "Memory", "text-right")}
                {header(null, "Net I/O", "text-right")}
                {header(null, "Disk I/O", "text-right")}
                {header("ip", "IP")}
                {header(null, "Ports")}
                {header("stack", "Stack")}
                {header(null, "Actions", "text-right")}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {rows.length === 0 && (
                <tr>
                  <td
                    colSpan={14}
                    className="px-2 py-4 text-center text-fg-muted"
                  >
                    {all.length
                      ? "No container matches the filters."
                      : "No containers."}
                  </td>
                </tr>
              )}
              {rows.map((c) => (
                <ContainerRow
                  key={c.id}
                  host={host}
                  c={c}
                  l={live[c.name]}
                  now={now}
                  manage={manage}
                  canExec={can("containers.exec")}
                  busy={busy === c.name}
                  onAct={(a) => void act(c, a)}
                  onDialog={(kind) => setDialog({ kind, c })}
                  onStack={setStackFilter}
                  upd={updates[c.name]}
                  onCheck={() => void check(c)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="text-[10px] text-fg-muted">
        CPU, memory and I/O refresh every {LIVE_REFRESH_MS / 1000} s; network
        and disk are totals since the container started.
      </p>
      {dialog?.kind === "logs" && (
        <LogsDialog host={host} c={dialog.c} onClose={() => setDialog(null)} />
      )}
      {dialog?.kind === "stats" && (
        <StatsDialog host={host} c={dialog.c} onClose={() => setDialog(null)} />
      )}
      {dialog?.kind === "exec" && (
        <ExecDialog host={host} c={dialog.c} onClose={() => setDialog(null)} />
      )}
    </div>
  );
}

type HostData = {
  containers: Container[];
  live: Record<string, Live>;
  error: string | null;
};

type AllRow = { host: Host; c: Container; l: Live | undefined };
const ALL_SORT: Getters<AllRow> = {
  host: (r) => r.host.node_name,
  name: (r) => r.c.name,
  image: (r) => r.c.image,
  state: (r) => r.c.state,
  health: (r) => r.c.health,
  uptime: (r) => (r.l?.started_at ? new Date(r.l.started_at).getTime() : null),
  restarts: (r) => r.l?.restarts,
  cpu: (r) => r.l?.cpu_pct,
  mem: (r) => r.l?.mem_bytes,
  ip: (r) => (r.c.ips[0] ?? "").split(".").map((p) => p.padStart(3, "0")).join("."),
  stack: (r) => r.c.project,
};

/** Every container of every Docker host in one table, with a host column. */
function AllHostsView({ hosts }: { hosts: Host[] }) {
  const { can } = useAuth();
  const manage = can("containers.manage");
  const canExec = can("containers.exec");
  const [data, setData] = useState<Record<string, HostData>>({});
  const [loaded, setLoaded] = useState(false);
  const [tick, setTick] = useState(0);
  // the refresh (button or an action) whose answer is on screen: the Refresh button spins until it catches up
  const [doneTick, setDoneTick] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [hostFilter, setHostFilter] = useState("");
  const [groupFilter, setGroupFilter] = useState("");
  const nodesRes = useResource<InventoryNode[]>("/nodes");
  const [dialog, setDialog] = useState<{
    kind: DialogKind;
    host: Host;
    c: Container;
  } | null>(null);
  const [notice, setNotice] = useState<{
    kind: "ok" | "error";
    text: string;
  } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      const entries = await Promise.all(
        hosts.map(async (h): Promise<[string, HostData]> => {
          const [cs, lv] = await Promise.all([
            api<Container[]>(`/containers/${h.node_id}`),
            api<Record<string, Live>>(`/containers/${h.node_id}/live`),
          ]);
          return [
            h.node_id,
            {
              containers: cs.ok ? cs.data : [],
              live: lv.ok ? lv.data : {},
              error: cs.ok ? null : message(cs.code),
            },
          ];
        }),
      );
      if (cancelled) return;
      setData(Object.fromEntries(entries));
      setLoaded(true);
      setDoneTick(tick);
      setNow(Date.now());
      timer = setTimeout(() => void load(), LIVE_REFRESH_MS);
    };
    void load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [hosts, tick]);

  async function act(h: Host, c: Container, action: string) {
    if (
      action === "remove" &&
      !window.confirm(`Remove the container ${c.name} on ${h.node_name}?`)
    )
      return;
    if (
      (action === "stop" || action === "kill") &&
      !window.confirm(`${action} ${c.name} on ${h.node_name}?`)
    )
      return;
    if (
      action === "update" &&
      !window.confirm(`Pull the image and recreate ${c.name} on ${h.node_name}?`)
    )
      return;
    setBusy(`${h.node_id}/${c.name}`);
    const r =
      action === "remove"
        ? await api(`/containers/${h.node_id}/c/${c.name}`, {
            method: "DELETE",
          })
        : await api(`/containers/${h.node_id}/c/${c.name}/${action}`, {
            method: "POST",
          });
    setBusy(null);
    setNotice(
      r.ok
        ? { kind: "ok", text: `${h.node_name} · ${c.name}: ${action} done.` }
        : { kind: "error", text: message(r.code) },
    );
    setTick((t) => t + 1);
  }

  const groupsOf = new Map((nodesRes.data ?? []).map((n) => [n.id, n.groups]));
  const groupNames = [...new Set([...groupsOf.values()].flat())].sort();
  const rows = hosts
    .filter((h) => !hostFilter || h.node_id === hostFilter)
    .filter((h) => !groupFilter || (groupsOf.get(h.node_id) ?? []).includes(groupFilter))
    .flatMap((h) =>
      (data[h.node_id]?.containers ?? []).map((c) => ({
        host: h,
        c,
        l: data[h.node_id]?.live[c.name],
      })),
    )
    .sort(
      (a, b) =>
        a.host.node_name.localeCompare(b.host.node_name) ||
        a.c.name.localeCompare(b.c.name),
    );
  const q = query.trim().toLowerCase();
  const visible = rows.filter(
    ({ host: h, c }) =>
      (!stateFilter || c.state === stateFilter) &&
      (!q ||
        [c.name, c.image, c.project ?? "", c.id, h.node_name, ...c.ips].some(
          (v) => v.toLowerCase().includes(q),
        )),
  );
  const { sorted, sort, toggle } = useSortedRows(visible, ALL_SORT);
  const total = rows.length;
  const states = [...new Set(rows.map((r) => r.c.state))].sort();
  const failed = hosts.filter((h) => data[h.node_id]?.error);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="rounded-full bg-elevated px-2 py-0.5 font-mono text-xs"
          title="Containers shown"
        >
          {visible.length}
          {visible.length !== total ? ` / ${total}` : ""}
        </span>
        <span className="font-mono text-[11px] text-fg-muted">
          {hosts.length} hosts
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-muted"
              aria-hidden
            />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search host, name, image, stack, IP…"
              aria-label="Search containers"
              className="w-60 rounded border border-line bg-canvas py-1.5 pl-7 pr-2 text-xs outline-none focus:border-primary"
            />
          </div>
          <select
            value={hostFilter}
            onChange={(e) => setHostFilter(e.target.value)}
            aria-label="Filter by host"
            className="rounded border border-line bg-canvas px-2 py-1.5 text-xs"
          >
            <option value="">All hosts</option>
            {hosts.map((h) => (
              <option key={h.node_id} value={h.node_id}>
                {h.node_name}
              </option>
            ))}
          </select>
          <select
            value={groupFilter}
            onChange={(e) => setGroupFilter(e.target.value)}
            aria-label="Filter by group"
            className="rounded border border-line bg-canvas px-2 py-1.5 text-xs"
          >
            <option value="">All groups</option>
            {groupNames.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </select>
          <select
            value={stateFilter}
            onChange={(e) => setStateFilter(e.target.value)}
            aria-label="Filter by state"
            className="rounded border border-line bg-canvas px-2 py-1.5 text-xs"
          >
            <option value="">All states</option>
            {states.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <Button onClick={() => setTick((t) => t + 1)}>
            <RefreshCw className={`h-3 w-3 ${loaded && doneTick !== tick ? "animate-spin" : ""}`} aria-hidden /> Refresh
          </Button>
        </div>
      </div>
      {notice && <Alert kind={notice.kind}>{notice.text}</Alert>}
      {!loaded && <Loading label={`Loading the containers of ${hosts.length} host${hosts.length === 1 ? "" : "s"}…`} />}
      {failed.length > 0 && (
        <Alert>
          {failed
            .map((h) => `${h.node_name}: ${data[h.node_id]?.error}`)
            .join(" · ")}
        </Alert>
      )}
      {loaded && (
        <div className="overflow-x-auto rounded-lg border border-line bg-surface">
          <table className="w-full text-xs">
            <thead className="bg-elevated text-left text-fg-secondary">
              <tr>
                {(
                  [
                    ["Host", "host"],
                    ["Name", "name"],
                    ["Image", "image"],
                    ["State", "state"],
                    ["Health", "health"],
                    ["Uptime", "uptime"],
                    ["Restarts", "restarts"],
                    ["CPU", "cpu"],
                    ["Memory", "mem"],
                    ["Net I/O", null],
                    ["Disk I/O", null],
                    ["IP", "ip"],
                    ["Ports", null],
                    ["Stack", "stack"],
                    ["Actions", null],
                  ] as const
                ).map(([label, col]) =>
                  col ? (
                    <SortTh key={label} label={label} col={col} sort={sort} onSort={toggle} className="whitespace-nowrap px-2 py-2 font-semibold" />
                  ) : (
                    <th key={label} className="whitespace-nowrap px-2 py-2 font-semibold">
                      {label}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {visible.length === 0 && (
                <tr>
                  <td
                    colSpan={15}
                    className="px-2 py-4 text-center text-fg-muted"
                  >
                    {total
                      ? "No container matches the filters."
                      : "No containers."}
                  </td>
                </tr>
              )}
              {sorted.map(({ host: h, c, l }) => (
                <ContainerRow
                  key={`${h.node_id}/${c.id}`}
                  host={h}
                  c={c}
                  l={l}
                  now={now}
                  showHost
                  manage={manage}
                  canExec={canExec}
                  busy={busy === `${h.node_id}/${c.name}`}
                  onAct={(a) => void act(h, c, a)}
                  onDialog={(kind) => setDialog({ kind, host: h, c })}
                  onStack={(name) => setQuery(name)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="text-[10px] text-fg-muted">
        CPU, memory and I/O refresh every {LIVE_REFRESH_MS / 1000} s; network
        and disk are totals since the container started.
      </p>
      {dialog?.kind === "logs" && (
        <LogsDialog
          host={dialog.host}
          c={dialog.c}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog?.kind === "stats" && (
        <StatsDialog
          host={dialog.host}
          c={dialog.c}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog?.kind === "exec" && (
        <ExecDialog
          host={dialog.host}
          c={dialog.c}
          onClose={() => setDialog(null)}
        />
      )}
    </div>
  );
}

const SECTIONS = [
  ["containers", "Containers"],
  ["stacks", "Stacks"],
  ["images", "Images"],
  ["volumes", "Volumes"],
  ["networks", "Networks"],
  ["disk", "Disk"],
] as const;
type Section = (typeof SECTIONS)[number][0];

function HostSections({ host }: { host: Host }) {
  const [section, setSection] = useState<Section>("containers");
  return (
    <div className="space-y-3">
      <div className="flex gap-1" role="tablist" aria-label="Docker resources">
        {SECTIONS.map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={section === key}
            onClick={() => setSection(key)}
            className={`rounded px-3 py-1 text-xs font-semibold ${section === key ? "bg-elevated text-fg" : "text-fg-muted hover:text-fg"}`}
          >
            {label}
          </button>
        ))}
      </div>
      {section === "containers" && <HostView host={host} />}
      {section === "stacks" && <StacksSection nodeId={host.node_id} />}
      {section === "images" && <ImagesSection nodeId={host.node_id} />}
      {section === "volumes" && <VolumesSection nodeId={host.node_id} />}
      {section === "networks" && <NetworksSection nodeId={host.node_id} />}
      {section === "disk" && <DiskSection nodeId={host.node_id} />}
    </div>
  );
}

export default function ContainersPage() {
  const hosts = useResource<Host[]>("/containers/hosts");
  const [selected, setSelected] = useState<string | null>(null);
  const list = hosts.data ?? [];
  const showAll = selected === ALL_HOSTS;
  const current = showAll
    ? undefined
    : (list.find((h) => h.node_id === selected) ?? list[0]);
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Containers</h1>
      {hosts.error && <Alert>{hosts.error}</Alert>}
      {hosts.data && list.length === 0 && (
        <p className="text-sm text-fg-muted">
          No Docker hosts. Give a node the Docker capability in the inventory:
          via agent (installed with “Allow Docker”) or via SSH (a user allowed
          to use Docker).
        </p>
      )}
      {list.length > 0 && (
        <div
          className="flex flex-wrap gap-1 border-b border-line"
          role="tablist"
        >
          <button
            role="tab"
            aria-selected={showAll}
            onClick={() => setSelected(ALL_HOSTS)}
            className={`-mb-px border-b-2 px-3 py-1.5 text-xs font-semibold ${showAll ? "border-primary text-primary" : "border-transparent text-fg-muted hover:text-fg"}`}
          >
            All hosts
          </button>
          {list.map((h) => (
            <button
              key={h.node_id}
              role="tab"
              aria-selected={current?.node_id === h.node_id}
              onClick={() => setSelected(h.node_id)}
              className={`-mb-px flex items-center gap-1.5 border-b-2 px-3 py-1.5 text-xs font-semibold ${current?.node_id === h.node_id ? "border-primary text-primary" : "border-transparent text-fg-muted hover:text-fg"}`}
            >
              <span
                className="inline-block h-1.5 w-1.5 rounded-full"
                style={{
                  background:
                    h.via === "ssh" || h.agent_online
                      ? "var(--color-ok)"
                      : "var(--color-crit)",
                }}
              />
              {h.node_name}
            </button>
          ))}
        </div>
      )}
      {showAll && <AllHostsView hosts={list} />}
      {current && <HostSections key={current.node_id} host={current} />}
    </div>
  );
}
