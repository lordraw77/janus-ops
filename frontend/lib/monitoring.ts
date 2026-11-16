/** Alerting API types (backend: janus_ops.monitoring). */

export type CheckState = "ok" | "warn" | "crit" | "unknown";
export type CheckKind = "host" | "metric" | "systemd" | "service" | "port" | "tcp" | "http" | "backup" | "snmp" | "fritzbox";

export type CheckDef = {
  node_id?: string;
  name: string;
  kind: CheckKind;
  params: Record<string, unknown>;
  comparison: "gt" | "lt" | null;
  warn_threshold: number | null;
  crit_threshold: number | null;
  check_cycles: number;
  interval_s: number;
  enabled?: boolean;
  source?: string;
};

export type Check = CheckDef & {
  id: string;
  node_id: string;
  node_name: string;
  enabled: boolean;
  source: string;
  state: CheckState | null;
  state_type: "soft" | "hard" | null;
  hard_state: CheckState | null;
  since: string | null;
  last_checked_at: string | null;
  last_output: string | null;
  is_flapping: boolean;
  unreachable: boolean;
  in_downtime: boolean;
};

export type Suggestion = { reason: string; check: CheckDef };

export type Problem = {
  id: string;
  check_id: string;
  check_name: string;
  check_kind: CheckKind;
  node_id: string;
  node_name: string;
  environment: string;
  state: CheckState;
  opened_at: string;
  closed_at: string | null;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  ack_comment: string | null;
  last_output: string | null;
  is_flapping: boolean;
  unreachable: boolean;
  in_downtime: boolean;
  notified: number;
};

export type Downtime = {
  id: string;
  scope: "node" | "check" | "group";
  target_id: string;
  target_name: string;
  starts_at: string;
  ends_at: string;
  reason: string;
  created_by: string | null;
  active: boolean;
};

export type ChannelKind = "email" | "telegram" | "webhook" | "discord" | "slack" | "ntfy";

export type Channel = {
  id: string;
  name: string;
  kind: ChannelKind;
  config: Record<string, unknown>;
  has_secret: boolean;
  enabled: boolean;
  rules: number;
};

export type Rule = {
  id: string;
  name: string;
  channel_id: string;
  channel_name: string;
  channel_kind: ChannelKind;
  min_state: "warn" | "unknown" | "crit";
  filter: { environments?: string[]; node_ids?: string[]; check_kinds?: CheckKind[] };
  escalate_after_s: number | null;
  position: number;
  enabled: boolean;
};

export type Delivery = {
  id: number;
  sent_at: string;
  status: "sent" | "failed";
  error: string | null;
  event: "problem" | "recovery";
  state: string | null;
  rule_name: string | null;
  channel_name: string | null;
  node_name: string;
  check_name: string;
};

export const KIND_LABEL: Record<CheckKind, string> = {
  host: "Agent connectivity",
  metric: "Metric threshold",
  systemd: "Systemd units",
  port: "Listening port (agent)",
  tcp: "TCP connect (from server)",
  http: "HTTP(S) request (from server)",
  backup: "Backups (Proxmox cluster)",
  snmp: "SNMP OID (from server)",
  service: "Systemd service (via agent)",
  fritzbox: "FRITZ!Box (TR-064, from server)",
};

export const CHANNEL_LABEL: Record<ChannelKind, string> = {
  email: "E-mail (SMTP)",
  telegram: "Telegram",
  webhook: "Webhook (JSON)",
  discord: "Discord",
  slack: "Slack",
  ntfy: "ntfy",
};

/** Human summary of what a check looks at. */
export function describeCheck(c: CheckDef): string {
  const p = c.params as Record<string, unknown>;
  const op = c.comparison === "lt" ? "<" : ">";
  const limits =
    c.comparison && (c.warn_threshold !== null || c.crit_threshold !== null)
      ? ` · warn ${op} ${c.warn_threshold ?? "–"}, crit ${op} ${c.crit_threshold ?? "–"}`
      : "";
  switch (c.kind) {
    case "metric": {
      const labels = Object.entries((p.labels as Record<string, string>) ?? {})
        .map(([k, v]) => `${k}=${v}`)
        .join(",");
      const win = p.window_s ? ` avg ${p.window_s}s` : "";
      return `${p.metric}${labels ? `{${labels}}` : ""}${win}${limits}`;
    }
    case "systemd":
      return p.unit ? `unit ${p.unit}` : "any failed unit";
    case "port":
      return `${p.proto ?? "tcp"}/${p.port} listening`;
    case "tcp":
      return `connect ${p.host ?? "node"}:${p.port}${limits}`;
    case "http":
      return `GET ${p.url} → ${p.expect_status ?? "200-399"}${limits}`;
    case "backup":
      return p.max_age_h ? `guests backed up within ${p.max_age_h} h` : "guests backed up on schedule";
    case "service":
      return `${p.unit} is active`;
    case "fritzbox":
      return `FRITZ!Box ${p.item ?? "item"}`;
    case "snmp":
      return `${p.oid}${p.scale && p.scale !== 1 ? ` × ${p.scale}` : ""}${limits}`;
    default:
      return "agent connected";
  }
}

export const DOWNTIME_PRESETS: [string, number][] = [
  ["30 min", 30],
  ["1 hour", 60],
  ["4 hours", 240],
  ["1 day", 1440],
];
