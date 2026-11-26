"use client";

import { ArrowDownAZ, ArrowUpAZ, FolderOpen, GripVertical, Server, Terminal } from "lucide-react";
import Link from "next/link";
import type { DragEvent, ReactNode } from "react";

import { SortTh } from "@/components/SortTh";
import { StateBadge } from "@/components/ui";
import { type Attention, type Dashboard, type NodeCardData, ago, formatDuration, formatValue } from "@/lib/agents";
import { useAuth } from "@/lib/auth";
import { type Direction, type Layout, METRICS, type Metric, type SortMode, directionLabel } from "@/lib/dashboardLayout";
import { type Getters, useSortedRows } from "@/lib/sort";
import { EnvBadge } from "@/components/EnvBadge";

const STATE_COLOR = {
  up: "var(--color-ok)",
  down: "var(--color-crit)",
  unreachable: "var(--color-warn)",
  unmonitored: "var(--color-offline)",
} as const;

/** The colour of one SNMP check's state. */
const CHECK_COLOR: Record<string, string> = {
  ok: "var(--color-ok)",
  warn: "var(--color-warn)",
  crit: "var(--color-crit)",
  unknown: "var(--color-offline)",
};


/** Host health: outer ring = share up, inner hexagon = worst state. */
export function HexagonHealth({ hosts, services, controls }: { hosts: Dashboard["hosts"]; services: Dashboard["services"]; controls?: ReactNode }) {
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
    <section className="rounded-lg border border-line bg-surface p-4 shadow-sm shadow-black/20">
      <div className="flex items-center gap-5">
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
      </div>
      <ServicesSummary services={services} />
      {controls}
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

/** The volume sizes from the NAS RAID MIB: only these are shown under the meters. */
const VOLUME_LABEL: Record<string, string> = {
  "1.3.6.1.4.1.6574.3.1.1.4.0": "Volume free",
  "1.3.6.1.4.1.6574.3.1.1.5.0": "Volume total",
};

const levelColor = (v: number | null) => (v === null ? undefined : v >= 95 ? "var(--color-crit)" : v >= 85 ? "var(--color-warn)" : undefined);

/** What the card needs to be dragged: only in the free order. */
export type Grip = { onDragStart: (e: DragEvent<HTMLElement>) => void; onDragEnd: () => void };

export function NodeCard({ node, compact = false, grip }: { node: NodeCardData; compact?: boolean; grip?: Grip }) {
  const { can } = useAuth();
  const l = node.latest;
  const volumes = compact ? [] : node.snmp.filter((r) => r.oid in VOLUME_LABEL);
  const time =
    node.state === "up" && l.uptime_s !== null ? `up ${formatDuration(l.uptime_s)}` : node.state !== "unmonitored" ? `seen ${ago(node.last_seen_at)}` : "";
  const actions = (
    <>
      {can("ssh.connect") && (
        <Link
          href={`/terminal/?node=${node.node_id}`}
          className={`rounded border border-line bg-elevated text-telemetry hover:border-line-strong ${compact ? "p-1" : "p-1.5"}`}
          title="Terminal"
        >
          <Terminal className="h-3.5 w-3.5" aria-label="Terminal" />
        </Link>
      )}
      {can("sftp.transfer") && (
        <Link
          href={`/terminal/?view=files&node=${node.node_id}`}
          className={`rounded border border-line bg-elevated text-primary hover:border-line-strong ${compact ? "p-1" : "p-1.5"}`}
          title="Files"
        >
          <FolderOpen className="h-3.5 w-3.5" aria-label="Files" />
        </Link>
      )}
    </>
  );
  return (
    <article
      className={`${compact ? "space-y-1.5 p-2" : "space-y-2.5 p-3"} rounded-lg border bg-surface shadow-sm shadow-black/20 transition-colors ${
        node.state === "down"
          ? "border-crit/50 hover:border-crit"
          : node.state === "unreachable"
            ? "border-warn/50 hover:border-warn"
            : "border-line hover:border-line-strong"
      }`}
    >
      <header className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1">
          {grip && (
            <span
              draggable
              onDragStart={grip.onDragStart}
              onDragEnd={grip.onDragEnd}
              className="-ml-1 shrink-0 cursor-grab touch-none rounded p-0.5 text-fg-muted hover:bg-elevated hover:text-fg active:cursor-grabbing"
              title="Drag to reorder"
              aria-label={`Drag ${node.name} to reorder`}
            >
              <GripVertical className="h-4 w-4" aria-hidden />
            </span>
          )}
          <Link href={`/nodes/?id=${node.node_id}`} className="flex min-w-0 items-center gap-2 hover:text-primary">
            <Server className="h-4 w-4 shrink-0 text-telemetry" aria-hidden />
            <span className="min-w-0">
              <span className="block truncate text-sm font-semibold">{node.name}</span>
              <span className="block truncate font-mono text-[11px] text-fg-muted">{node.address}</span>
            </span>
          </Link>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <EnvBadge env={node.environment} />
          {compact && actions}
        </div>
      </header>
      <div className="flex items-center gap-2 text-xs">
        <span className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: STATE_COLOR[node.state] }} aria-hidden />
        <span>{STATE_LABEL[node.state]}</span>
        {node.downtime && (
          <span className="rounded border border-telemetry/40 px-1 font-mono text-[9px] text-telemetry">DOWNTIME</span>
        )}
        <span className={`font-mono text-[11px] text-fg-muted ${compact ? "truncate" : "ml-auto"}`}>{time}</span>
        {compact && node.state === "up" && (
          <span className="ml-auto flex shrink-0 items-center gap-2.5 font-mono text-[11px] tabular-nums text-fg-secondary">
            <span title="CPU">
              <span className="text-fg-muted">cpu</span> <span style={{ color: levelColor(l.cpu_pct) }}>{l.cpu_pct === null ? "–" : `${l.cpu_pct.toFixed(0)}%`}</span>
            </span>
            <span title={l.disk_mount ? `Disk ${l.disk_mount}` : "Disk"}>
              <span className="text-fg-muted">disk</span> <span style={{ color: levelColor(l.disk_pct) }}>{l.disk_pct === null ? "–" : `${l.disk_pct.toFixed(0)}%`}</span>
            </span>
            <span title="Load, 1 minute">
              <span className="text-fg-muted">load</span> {l.load1?.toFixed(2) ?? "–"}
            </span>
            {(l.failed_units ?? 0) > 0 && (
              <span className="text-warn" title={`${l.failed_units} failed units`}>
                ⚠{l.failed_units}
              </span>
            )}
          </span>
        )}
      </div>
      {!compact && node.state === "up" && (
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
      {volumes.length > 0 && (
        <div className="space-y-1 border-t border-line pt-2">
          {volumes.map((r) => (
            <div key={r.oid} className="flex items-center gap-2 text-[11px]">
              <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: CHECK_COLOR[r.state] ?? "var(--color-offline)" }} aria-hidden />
              <span className="min-w-0 flex-1 truncate text-fg-muted">{VOLUME_LABEL[r.oid] ?? r.name}</span>
              <span className="font-mono">{r.value !== null ? formatValue(r.value, "B") : (r.text ?? "–")}</span>
            </div>
          ))}
        </div>
      )}
      {!compact && <footer className="flex gap-1">{actions}</footer>}
    </article>
  );
}

function Segment<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: { id: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="flex items-center gap-2">
      <span className="w-12 shrink-0 text-[10px] font-semibold uppercase tracking-wider text-fg-muted">{label}</span>
      <div role="group" aria-label={label} className="flex overflow-hidden rounded border border-line text-[11px]">
        {options.map((o) => (
          <button
            key={o.id}
            type="button"
            aria-pressed={value === o.id}
            onClick={() => onChange(o.id)}
            className={`px-2 py-1 ${value === o.id ? "bg-primary/15 font-semibold text-primary" : "text-fg-secondary hover:bg-elevated hover:text-fg"}`}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

/** The choice of order and view of the node cards, shown in the health panel. */
export function CardControls({ layout, onChange }: { layout: Layout; onChange: (next: Layout) => void }) {
  const set = (patch: Partial<Layout>) => onChange({ ...layout, ...patch });
  const DirIcon = layout.dir === "asc" ? ArrowDownAZ : ArrowUpAZ;
  return (
    <div className="mt-3 space-y-2 border-t border-line pt-3" aria-label="Node cards">
      <Segment<SortMode>
        label="Order"
        value={layout.mode}
        options={[
          { id: "name", label: "Name" },
          { id: "load", label: "Load" },
          { id: "custom", label: "Free" },
        ]}
        onChange={(mode) => set({ mode, dir: mode === "load" && layout.mode !== "load" ? "desc" : mode === "name" && layout.mode !== "name" ? "asc" : layout.dir })}
      />
      {layout.mode !== "custom" ? (
        <div className="flex flex-wrap items-center gap-2 pl-14">
          {layout.mode === "load" && (
            <select
              value={layout.metric}
              onChange={(e) => set({ metric: e.target.value as Metric })}
              aria-label="Metric to order by"
              className="rounded border border-line bg-canvas px-1.5 py-1 text-[11px] hover:border-line-strong"
            >
              {METRICS.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
          )}
          <button
            type="button"
            onClick={() => set({ dir: (layout.dir === "asc" ? "desc" : "asc") as Direction })}
            className="flex items-center gap-1 rounded border border-line px-2 py-1 text-[11px] text-fg-secondary hover:border-line-strong hover:text-fg"
            title="Reverse the order"
          >
            <DirIcon className="h-3.5 w-3.5" aria-hidden /> {directionLabel(layout.mode, layout.dir)}
          </button>
        </div>
      ) : (
        <p className="pl-14 text-[11px] text-fg-muted">
          Drag a card by its handle.{layout.order.length > 0 && (
            <button type="button" onClick={() => set({ order: [] })} className="ml-1 text-primary hover:text-primary-hover">
              Reset
            </button>
          )}
        </p>
      )}
      <Segment<"full" | "compact">
        label="Cards"
        value={layout.compact ? "compact" : "full"}
        options={[
          { id: "full", label: "Expanded" },
          { id: "compact", label: "Compact" },
        ]}
        onChange={(v) => set({ compact: v === "compact" })}
      />
    </div>
  );
}

const SEVERITY: Record<string, number> = { crit: 0, unknown: 1, warn: 2, ok: 3 };
const ATTENTION_SORT: Getters<Attention> = {
  state: (a) => SEVERITY[a.severity] ?? 9,
  node: (a) => `${a.node_name} / ${a.check_name}`,
  output: (a) => a.message,
  since: (a) => a.since,
};

export function AttentionTable({ items, handled }: { items: Attention[]; handled: number }) {
  const { sorted, sort, toggle } = useSortedRows(items, ATTENTION_SORT);
  return (
    <section className="flex max-h-[19.5rem] min-h-0 flex-col rounded-lg border border-line bg-surface p-4 shadow-sm shadow-black/20">
      <div className="mb-2 flex shrink-0 items-center justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-fg-muted">Unhandled problems</h2>
        <Link href="/problems/" className="text-[11px] text-primary hover:text-primary-hover">
          {handled > 0 ? `+${handled} handled · ` : ""}All problems →
        </Link>
      </div>
      {items.length === 0 ? (
        <p className="flex items-center gap-2 text-sm text-ok"><span className="inline-block h-2 w-2 rounded-full bg-ok" aria-hidden />All clear — nothing needs attention.</p>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto pr-1" tabIndex={0} aria-label="Unhandled problems, scrollable">
        <table className="w-full text-sm">
          <thead className="sticky top-0 z-10 bg-surface text-left text-[11px] uppercase text-fg-muted">
            <tr>
              <SortTh label="State" col="state" sort={sort} onSort={toggle} className="py-1 pr-3 font-medium" />
              <SortTh label="Node / check" col="node" sort={sort} onSort={toggle} className="py-1 pr-3 font-medium" />
              <SortTh label="Output" col="output" sort={sort} onSort={toggle} className="py-1 pr-3 font-medium" />
              <SortTh label="Since" col="since" sort={sort} onSort={toggle} className="py-1 text-right font-medium" />
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {sorted.map((a) => (
              <tr key={a.problem_id} className="align-top transition-colors hover:bg-elevated/50">
                <td className="py-1.5 pr-2">
                  <StateBadge state={a.severity} />
                </td>
                <td className="py-1.5 pr-3">
                  <Link href={`/nodes/?id=${a.node_id}`} className="hover:text-primary">
                    {a.node_name}
                  </Link>
                  <span className="text-fg-secondary"> / {a.check_name}</span>
                  {a.flapping && <span className="ml-1 rounded border border-unknown/40 px-1 font-mono text-[9px] text-unknown">FLAPPING</span>}
                </td>
                <td className="py-1.5 pr-3 text-xs text-fg-secondary">{a.message}</td>
                <td className="whitespace-nowrap py-1.5 text-right font-mono text-[11px] text-fg-muted">{ago(a.since)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </section>
  );
}

export function ServicesSummary({ services }: { services: Dashboard["services"] }) {
  const rows: [string, number, string][] = [
    ["OK", services.ok, "var(--color-ok)"],
    ["Warn", services.warn, "var(--color-warn)"],
    ["Crit", services.crit, "var(--color-crit)"],
    ["Unknown", services.unknown, "var(--color-unknown)"],
    ["Pending", services.pending, "var(--color-offline)"],
  ];
  return (
    <dl className="mt-3 grid grid-cols-5 gap-2 border-t border-line pt-3 text-center font-mono text-xs">
      {rows.map(([label, value, color]) => (
        <div key={label}>
          <dd className="text-base font-bold tabular-nums" style={{ color: value > 0 && label !== "OK" && label !== "Pending" ? color : undefined }}>
            {value}
          </dd>
          <dt className="flex items-center justify-center gap-1 text-[10px] text-fg-muted">
            <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: color }} />
            {label}
          </dt>
        </div>
      ))}
    </dl>
  );
}
