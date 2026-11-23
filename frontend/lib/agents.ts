/** Agent and telemetry API types (backend: janus_ops.agents) and value formatting. */

export type AgentStatus = "online" | "offline" | "revoked" | "pending" | "not_installed";

export type Agent = {
  node_id: string;
  node_name: string;
  address: string;
  environment: string;
  parent_id: string | null;
  capability: boolean;
  capability_enabled: boolean;
  has_ssh: boolean;
  status: AgentStatus;
  key_fingerprint: string | null;
  version: string | null;
  outdated: boolean;
  arch: string | null;
  os: string | null;
  kernel: string | null;
  hostname: string | null;
  policy: { allow_exec: boolean; allow_pty: boolean; allow_docker: boolean } | null;
  interval_s: number | null;
  enrolled_at: string | null;
  last_seen_at: string | null;
  revoked_at: string | null;
  connected_since: string | null;
  remote_ip: string | null;
};

export type Proc = { pid: number; name: string; cmd?: string; user?: string; cpu_pct: number; rss_bytes: number };
export type Listener = { proto: string; addr: string; port: number; process: string };

/** A well-known service the agent found, with the settings it read from the configuration. */
export type FoundService = { name: string; process: string; cmd?: string; config?: Record<string, string> };

export type AgentDetail = Agent & {
  facts: {
    uptime_s: number;
    failed_units: string[];
    services?: FoundService[];
    listening: Listener[];
    top_cpu: Proc[];
    top_mem: Proc[];
  } | null;
  facts_at: string | null;
};

export type HostState = "up" | "down" | "unreachable" | "unmonitored";

/** The latest result of one SNMP check of a node. */
export type SnmpReading = {
  name: string;
  oid: string;
  state: string;
  value: number | null;
  text: string | null;
};

export type NodeCardData = {
  node_id: string;
  name: string;
  address: string;
  environment: string;
  parent_id: string | null;
  agent: AgentStatus | "none";
  state: HostState;
  downtime: boolean;
  last_seen_at: string | null;
  latest: {
    cpu_pct: number | null;
    cpu_count: number | null;
    load1: number | null;
    mem_pct: number | null;
    swap_pct: number | null;
    disk_pct: number | null;
    disk_mount: string | null;
    uptime_s: number | null;
    failed_units: number | null;
  };
  cpu_spark: (number | null)[];
  snmp: SnmpReading[];
};

export type Attention = {
  problem_id: string;
  node_id: string;
  node_name: string;
  check_name: string;
  severity: "warn" | "crit" | "unknown";
  message: string;
  since: string;
  flapping: boolean;
};

export type Dashboard = {
  generated_at: string;
  hosts: Record<HostState | "downtime", number>;
  services: Record<"ok" | "warn" | "crit" | "unknown" | "pending", number>;
  handled: number;
  nodes: NodeCardData[];
  attention: Attention[];
};

export type SeriesPoint = { t: number; v: number; max: number };
export type Series = { name: string; labels: Record<string, string>; unit: string | null; points: SeriesPoint[] };
export type Chart = { node_id: string; range: string; bucket_s: number; series: Series[] };

export const AGENT_STATUS: Record<AgentStatus, { label: string; className: string }> = {
  online: { label: "Online", className: "border-ok/40 text-ok" },
  offline: { label: "Offline", className: "border-crit/40 text-crit" },
  revoked: { label: "Revoked", className: "border-offline text-fg-muted" },
  pending: { label: "Installing", className: "border-warn/40 text-warn" },
  not_installed: { label: "Not installed", className: "border-line text-fg-muted" },
};

const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];

export function formatBytes(v: number, perSecond = false): string {
  let i = 0;
  let x = Math.abs(v);
  while (x >= 1024 && i < BYTE_UNITS.length - 1) {
    x /= 1024;
    i++;
  }
  const digits = x >= 100 || i === 0 ? 0 : x >= 10 ? 1 : 2;
  return `${(v < 0 ? -x : x).toFixed(digits)} ${BYTE_UNITS[i]}${perSecond ? "/s" : ""}`;
}

/** A value in the unit reported by the server ("%", "B", "B/s", "s" or none). */
export function formatValue(v: number, unit: string | null): string {
  if (unit === "%") return `${v === 0 || v >= 10 ? v.toFixed(0) : v.toFixed(1)}%`;
  if (unit === "B") return formatBytes(v);
  if (unit === "B/s") return formatBytes(v, true);
  if (unit === "s") return formatDuration(v);
  return v === 0 || v >= 100 ? v.toFixed(0) : v.toFixed(2);
}

export function formatDuration(seconds: number): string {
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function ago(iso: string | null, now = Date.now()): string {
  if (!iso) return "never";
  const s = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  return `${formatDuration(s)} ago`;
}
