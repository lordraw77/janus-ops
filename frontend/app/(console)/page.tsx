"use client";

import { useEffect, useState } from "react";

import Link from "next/link";

import { type Status, StatusBadge } from "@/components/StatusBadge";
import { useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { CAPABILITIES, type CapabilityKind, ENVIRONMENTS, type InventoryNode } from "@/lib/inventory";

const POLL_MS = 15_000;

type ReadyResponse = {
  status: "ok" | "unavailable";
  version: string;
  checks: Record<string, string>;
};

type Probe =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "loaded"; data: ReadyResponse; at: Date };

const CHECK_LABELS: Record<string, string> = {
  database: "PostgreSQL",
  timescaledb: "TimescaleDB extension",
  schema: "Database schema",
};

function checkStatus(value: string): Status {
  return value === "ok" ? "ok" : "crit";
}

function InventorySummary() {
  const { data: nodes, error } = useResource<InventoryNode[]>("/nodes");
  if (error) return null;
  const count = (pred: (n: InventoryNode) => boolean) => (nodes ?? []).filter(pred).length;
  return (
    <section className="rounded-lg border border-line bg-surface p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-fg-muted">Inventory</h2>
        <Link href="/inventory/" className="text-[11px] text-primary hover:text-primary-hover">
          Open →
        </Link>
      </div>
      {!nodes ? (
        <p className="text-sm text-fg-muted">Loading…</p>
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-[auto_1fr_1fr]">
          <div>
            <div className="font-mono text-3xl font-bold tabular-nums">{nodes.length}</div>
            <div className="text-[11px] text-fg-muted">nodes</div>
          </div>
          <dl className="space-y-1 font-mono text-xs">
            {ENVIRONMENTS.map((env) => (
              <div key={env} className="flex justify-between gap-3">
                <dt className="text-fg-muted">{env}</dt>
                <dd className="tabular-nums">{count((n) => n.environment === env)}</dd>
              </div>
            ))}
          </dl>
          <dl className="space-y-1 font-mono text-xs">
            {(Object.keys(CAPABILITIES) as CapabilityKind[]).map((k) => (
              <div key={k} className="flex justify-between gap-3">
                <dt className="text-fg-muted">{CAPABILITIES[k].label}</dt>
                <dd className="tabular-nums">
                  {count((n) => n.capabilities.some((c) => c.kind === k && c.enabled))}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </section>
  );
}

export default function DashboardPage() {
  const { can } = useAuth();
  const [probe, setProbe] = useState<Probe>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        // /ready answers 503 with a JSON body when a dependency is down: both are valid here
        const res = await fetch("/api/v1/ready", { cache: "no-store" });
        const data = (await res.json()) as ReadyResponse;
        if (!cancelled) setProbe({ kind: "loaded", data, at: new Date() });
      } catch {
        if (!cancelled) setProbe({ kind: "error" });
      }
    }

    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <div className="max-w-3xl space-y-4">
      <h1 className="text-lg font-semibold">Dashboard</h1>

      <section className="rounded-lg border border-line bg-surface p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-fg-muted">
            Control plane
          </h2>
          {probe.kind === "loaded" && (
            <span className="font-mono text-[11px] text-fg-muted">
              v{probe.data.version} · checked {probe.at.toLocaleTimeString()}
            </span>
          )}
        </div>

        {probe.kind === "loading" && <p className="text-sm text-fg-muted">Checking…</p>}

        {probe.kind === "error" && (
          <div className="flex items-center justify-between text-sm">
            <span>API not reachable</span>
            <StatusBadge status="offline" />
          </div>
        )}

        {probe.kind === "loaded" && (
          <ul className="divide-y divide-line text-sm">
            {Object.entries(probe.data.checks).map(([name, value]) => (
              <li key={name} className="flex items-center justify-between py-2">
                <span>
                  {CHECK_LABELS[name] ?? name}
                  {value !== "ok" && (
                    <span className="ml-2 font-mono text-xs text-fg-muted">{value}</span>
                  )}
                </span>
                <StatusBadge status={checkStatus(value)} />
              </li>
            ))}
          </ul>
        )}
      </section>

      {can("inventory.read") && <InventorySummary />}

      <p className="text-xs text-fg-muted">
        NOC widgets (host and service hexagons, node cards, unhandled problems) arrive with
        phases F2–F3.
      </p>
    </div>
  );
}
