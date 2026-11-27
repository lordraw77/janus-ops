/** LAN watch: what the /lan API returns, and the small calculations the page needs. */

export type LanSubnet = {
  id: string;
  name: string;
  cidr: string;
  interval_minutes: number;
  enabled: boolean;
  last_run_at: string | null;
  last_found: number | null;
  last_error: string | null;
  hosts_online: number;
  hosts_total: number;
  sweeping: boolean;
  /** a sweep was asked for and the scanner has not started it yet */
  requested: boolean;
  last_new: number | null;
};

export type LanScanner = { mode: "local" | "external"; running: boolean; seen_at: string | null };

export type LanHost = {
  id: string;
  ip: string;
  mac: string;
  hostname: string | null;
  label: string | null;
  subnet: string | null;
  online: boolean;
  since: string;
  first_seen: string;
  last_seen: string;
  macs_on_ip: number;
  ips_of_mac: number;
  node_name: string | null;
  node_id: string | null;
};

export type LanEventKind = "new" | "online" | "offline" | "mac_changed" | "ip_changed";

export type LanEvent = {
  id: number;
  at: string;
  kind: LanEventKind;
  prev: string | null;
  host_id: string;
  ip: string;
  mac: string;
  hostname: string | null;
  label: string | null;
};

export type LanSegment = { start: string; end: string; online: boolean };

export type LanHistory = {
  start: string;
  end: string;
  segments: LanSegment[];
  uptime_pct: number | null;
  events: LanEvent[];
  siblings: { id: string; ip: string; mac: string; online: boolean; last_seen: string }[];
};

export const KIND_LABEL: Record<LanEventKind, string> = {
  new: "First seen",
  online: "Came online",
  offline: "Went offline",
  mac_changed: "MAC changed",
  ip_changed: "Address changed",
};

/** What a `prev` value is, per event kind (the old MAC or the old address). */
export function prevLabel(kind: LanEventKind): string | null {
  return kind === "mac_changed" ? "was" : kind === "ip_changed" ? "was at" : null;
}

/** The name to show for a host: the label a person gave it, else its DNS name, else nothing. */
export function displayName(h: { label: string | null; hostname: string | null }): string | null {
  return h.label || h.hostname || null;
}

/** "5 min", "2 h 10 min", "3 d 4 h": how long ago or for how long, to two units. */
export function span(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
}

export type Bar = { left: number; width: number; online: boolean; from: string; to: string };

/** The stretches of a period as percentages of its width, for drawing the availability strip. */
export function bars(segments: LanSegment[], start: string, end: string): Bar[] {
  const t0 = Date.parse(start);
  const total = Date.parse(end) - t0;
  if (!(total > 0)) return [];
  return segments
    .map((s) => {
      const from = Math.max(Date.parse(s.start), t0);
      const to = Math.min(Date.parse(s.end), t0 + total);
      return { left: ((from - t0) / total) * 100, width: ((to - from) / total) * 100, online: s.online, from: s.start, to: s.end };
    })
    .filter((b) => b.width > 0);
}

/** Does a host match what was typed (address, MAC, name, label, subnet)? */
export function matches(h: LanHost, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [h.ip, h.mac, h.hostname, h.label, h.subnet, h.node_name].some((v) => v?.toLowerCase().includes(q));
}
