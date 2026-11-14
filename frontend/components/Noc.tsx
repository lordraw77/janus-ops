"use client";

import { FolderOpen, Server, Terminal } from "lucide-react";
import Link from "next/link";

import { type Attention, type Dashboard, type NodeCardData, ago, formatDuration } from "@/lib/agents";
import { useAuth } from "@/lib/auth";
import { ENV_STYLE } from "@/lib/inventory";

const STATE_COLOR = {
  up: "var(--color-ok)",
  down: "var(--color-crit)",
  unreachable: "var(--color-warn)",
  unmonitored: "var(--color-offline)",
} as const;

/** Host health: outer ring = share up, inner hexagon = worst state. */
export function HexagonHealth({ hosts }: { hosts: Dashboard["hosts"] }) {
  const monitored = hosts.up + hosts.down + hosts.unreachable;
  const pct = monitored > 0 ? Math.round((hosts.up / monitored) * 100) : null;
  const worst = hosts.down > 0 ? "crit" : hosts.unreachable > 0 ? "warn" : monitored > 0 ? "ok" : "offline";
  const stroke = `var(--color-${worst})`;
  const rows: { label: string; value: number; color: string }[] = [
    { label: "Up", value: hosts.up, color: "var(--color-ok)" },
    { label: "Down", value: hosts.down, color: "var(--color-crit)" },
    { label: "Unreachable", value: hosts.unreachable, color: "var(--color-warn)" },
    { label: "Downtime", value: hosts.downtime, color: "var(--color-telemetry)" },
    { label: "No agent", value: hosts.unmonitored, color: "var(--color-offline)" },
  ];
  return (
    <section className="flex items-center gap-5 rounded-lg border border-line bg-surface p-4">
      <div className="relative h-28 w-28 shrink-0">
        <svg viewBox="0 0 120 120" className="h-full w-full" aria-hidden>
          <polygon points="60,6 108,33 108,87 60,114 12,87 12,33" fill="none" stroke="var(--color-line-strong)" strokeWidth="4" />
          <polygon
            points="60,22 94,41 94,79 60,98 26,79 26,41"
            fill={stroke}
            fillOpacity={0.14}
            stroke={stroke}
            strokeWidth="3"
          />
        </svg>
        <span className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="font-mono text-lg font-bold tabular-nums">{pct === null ? "–" : `${pct}%`}</span>
          <span className="text-[9px] uppercase text-fg-muted">hosts up</span>
        </span>
      </div>
      <dl className="flex-1 space-y-1 font-mono text-xs">
        {rows.map((r) => (
          <div key={r.label} className="flex items-center justify-between gap-3">
            <dt className="flex items-center gap-2 text-fg-secondary">
              <span className="inline-block h-2 w-2 rounded-full" style={{ background: r.color }} />
              {r.label}
            </dt>
            <dd className="tabular-nums text-fg">{r.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function Sparkline({ values }: { values: (number | null)[] }) {
  if (values.every((v) => v === null)) return <div className="h-6" />;
  const w = 120;
  const h = 24;
  let d = "";
  values.forEach((v, i) => {
    if (v === null) return;
    const px = (i / Math.max(1, values.length - 1)) * w;
    const py = h - 1 - (Math.min(100, v) / 100) * (h - 2);
    d += `${d === "" || values[i - 1] === null ? "M" : "L"}${px.toFixed(1)},${py.toFixed(1)}`;
  });
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-6 w-full" preserveAspectRatio="none" aria-label="CPU, last 30 minutes">
      <path d={d} fill="none" stroke="var(--color-telemetry)" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function Meter({ label, value, detail }: { label: string; value: number | null; detail?: string }) {
  const level = value === null ? "offline" : value >= 95 ? "crit" : value >= 85 ? "warn" : "ok";
  return (
    <div>
      <div className="flex justify-between text-[11px]">
        <span className="text-fg-muted">
          {label}
          {detail && <span className="ml-1 text-fg-muted">{detail}</span>}
        </span>
        <span className="font-mono tabular-nums text-fg">{value === null ? "–" : `${value.toFixed(0)}%`}</span>
      </div>
      <div className="mt-0.5 h-1.5 w-full overflow-hidden rounded-full bg-elevated">
        <div
          className="h-full rounded-full"
          style={{ width: `${Math.min(100, value ?? 0)}%`, background: `var(--color-${level === "ok" ? "telemetry" : level})` }}
        />
      </div>
    </div>
  );
}

const STATE_LABEL = { up: "Up", down: "Down", unreachable: "Unreachable", unmonitored: "No agent" };

export function NodeCard({ node }: { node: NodeCardData }) {
  const { can } = useAuth();
  const l = node.latest;
  return (
    <article className="space-y-2.5 rounded-lg border border-line bg-surface p-3">
      <header className="flex items-start justify-between gap-2">
        <Link href={`/nodes/?id=${node.node_id}`} className="flex min-w-0 items-center gap-2 hover:text-primary">
          <Server className="h-4 w-4 shrink-0 text-telemetry" aria-hidden />
          <span className="min-w-0">
            <span className="block truncate text-sm font-semibold">{node.name}</span>
            <span className="block truncate font-mono text-[11px] text-fg-muted">{node.address}</span>
          </span>
        </Link>
        <span className={`shrink-0 rounded border px-1.5 text-[10px] uppercase ${ENV_STYLE[node.environment] ?? ""}`}>
          {node.environment}
        </span>
      </header>
      <div className="flex items-center gap-2 text-xs">
        <span className="inline-block h-2 w-2 rounded-full" style={{ background: STATE_COLOR[node.state] }} aria-hidden />
        <span>{STATE_LABEL[node.state]}</span>
        <span className="ml-auto font-mono text-[11px] text-fg-muted">
          {node.state === "up" && l.uptime_s !== null
            ? `up ${formatDuration(l.uptime_s)}`
            : node.state !== "unmonitored"
              ? `seen ${ago(node.last_seen_at)}`
              : ""}
        </span>
      </div>
      {node.state === "up" && (
        <>
          <Meter label="CPU" value={l.cpu_pct} detail={l.cpu_count ? `${l.cpu_count} cores` : undefined} />
          <Sparkline values={node.cpu_spark} />
          <Meter label="RAM" value={l.mem_pct} />
          <Meter label="Disk" value={l.disk_pct} detail={l.disk_mount ?? undefined} />
          <div className="flex justify-between font-mono text-[11px] text-fg-muted">
            <span>load {l.load1?.toFixed(2) ?? "–"}</span>
            {(l.failed_units ?? 0) > 0 && <span className="text-warn">{l.failed_units} failed units</span>}
          </div>
        </>
      )}
      <footer className="flex gap-1">
        {can("ssh.connect") && (
          <Link
            href={`/terminal/?node=${node.node_id}`}
            className="rounded border border-line bg-elevated p-1.5 text-telemetry hover:border-line-strong"
            title="Terminal"
          >
            <Terminal className="h-3.5 w-3.5" aria-label="Terminal" />
          </Link>
        )}
        {can("sftp.transfer") && (
          <Link
            href={`/terminal/?view=files&node=${node.node_id}`}
            className="rounded border border-line bg-elevated p-1.5 text-primary hover:border-line-strong"
            title="Files"
          >
            <FolderOpen className="h-3.5 w-3.5" aria-label="Files" />
          </Link>
        )}
      </footer>
    </article>
  );
}

export function AttentionTable({ items }: { items: Attention[] }) {
  return (
    <section className="rounded-lg border border-line bg-surface p-4">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wider text-fg-muted">Needs attention</h2>
      {items.length === 0 ? (
        <p className="text-sm text-fg-muted">Nothing to report.</p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-[11px] uppercase text-fg-muted">
            <tr>
              <th className="py-1 font-medium">State</th>
              <th className="py-1 font-medium">Node</th>
              <th className="py-1 font-medium">Problem</th>
              <th className="py-1 text-right font-medium">Since</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {items.map((a) => (
              <tr key={`${a.node_id}-${a.kind}`}>
                <td className="py-1.5">
                  <span
                    className={`rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold ${a.severity === "crit" ? "bg-crit text-white" : "bg-warn text-canvas"}`}
                  >
                    {a.severity.toUpperCase()}
                  </span>
                </td>
                <td className="py-1.5">
                  <Link href={`/nodes/?id=${a.node_id}`} className="hover:text-primary">
                    {a.node_name}
                  </Link>
                </td>
                <td className="py-1.5 text-fg-secondary">{a.message}</td>
                <td className="py-1.5 text-right font-mono text-[11px] text-fg-muted">{a.since ? ago(a.since) : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="mt-2 text-[10px] text-fg-muted">
        Fixed thresholds until the check engine (F3): disk ≥ 85/95 %, memory ≥ 90/97 %, failed systemd
        units, agent offline. No notifications are sent yet.
      </p>
    </section>
  );
}
