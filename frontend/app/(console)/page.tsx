"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { AttentionTable, HexagonHealth, NodeCard } from "@/components/Noc";
import { StatusBadge } from "@/components/StatusBadge";
import { Alert } from "@/components/ui";
import type { Dashboard } from "@/lib/agents";
import { api, message } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { ENVIRONMENTS } from "@/lib/inventory";

const POLL_MS = 10_000;

type ReadyResponse = { status: "ok" | "unavailable"; version: string; checks: Record<string, string> };

const CHECK_LABELS: Record<string, string> = {
  database: "PostgreSQL",
  timescaledb: "TimescaleDB",
  schema: "Schema",
};

/** Poll `load` every POLL_MS; state only changes in the async callbacks. */
function usePoll<T>(load: () => Promise<T>): T | null {
  const [value, setValue] = useState<T | null>(null);
  useEffect(() => {
    let cancelled = false;
    const tick = () =>
      void load().then((v) => {
        if (!cancelled) setValue(v);
      });
    tick();
    const timer = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [load]);
  return value;
}

async function loadReady(): Promise<ReadyResponse | "error"> {
  try {
    // /ready answers 503 with a JSON body when a dependency is down: both are valid here
    const res = await fetch("/api/v1/ready", { cache: "no-store" });
    return (await res.json()) as ReadyResponse;
  } catch {
    return "error";
  }
}

async function loadDashboard(): Promise<{ data: Dashboard | null; error: string | null }> {
  const r = await api<Dashboard>("/telemetry/dashboard");
  return r.ok ? { data: r.data, error: null } : { data: null, error: message(r.code) };
}

function ControlPlane() {
  const ready = usePoll(loadReady);
  return (
    <section className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-lg border border-line bg-surface px-4 py-2.5 text-xs">
      <span className="font-semibold uppercase tracking-wider text-fg-muted">Control plane</span>
      {ready === null && <span className="text-fg-muted">Checking…</span>}
      {ready === "error" && (
        <span className="flex items-center gap-2">
          API not reachable <StatusBadge status="offline" />
        </span>
      )}
      {ready !== null && ready !== "error" && (
        <>
          {Object.entries(ready.checks).map(([name, value]) => (
            <span key={name} className="flex items-center gap-2">
              {CHECK_LABELS[name] ?? name}
              <StatusBadge status={value === "ok" ? "ok" : "crit"} />
            </span>
          ))}
          <span className="ml-auto font-mono text-[11px] text-fg-muted">v{ready.version}</span>
        </>
      )}
    </section>
  );
}

function Noc() {
  const result = usePoll(loadDashboard);
  if (result === null) return <p className="text-sm text-fg-muted">Loading…</p>;
  if (result.error || !result.data) return <Alert>{result.error}</Alert>;
  const dash = result.data;
  const order = (env: string) => ENVIRONMENTS.indexOf(env as (typeof ENVIRONMENTS)[number]);
  const nodes = [...dash.nodes].sort(
    (a, b) =>
      Number(a.state === "unmonitored") - Number(b.state === "unmonitored") ||
      order(a.environment) - order(b.environment) ||
      a.name.localeCompare(b.name),
  );
  return (
    <>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_1fr]">
        <HexagonHealth hosts={dash.hosts} services={dash.services} />
        <AttentionTable items={dash.attention} handled={dash.handled} />
      </div>
      {nodes.length === 0 ? (
        <p className="text-sm text-fg-muted">
          No nodes yet. Add them in the <Link href="/inventory/" className="text-primary">inventory</Link>,
          enable the Janus agent capability and install it from the node page.
        </p>
      ) : (
        <div className="grid items-start gap-3 sm:grid-cols-2 xl:grid-cols-4 2xl:grid-cols-5">
          {nodes.map((n) => (
            <NodeCard key={n.node_id} node={n} />
          ))}
        </div>
      )}
      <p className="font-mono text-[10px] text-fg-muted">
        updated {new Date(dash.generated_at).toLocaleTimeString()} · refresh every {POLL_MS / 1000}s
      </p>
    </>
  );
}

export default function DashboardPage() {
  const { can } = useAuth();
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Dashboard</h1>
      <ControlPlane />
      {can("inventory.read") ? (
        <Noc />
      ) : (
        <p className="text-sm text-fg-muted">The NOC view needs the inventory.read permission.</p>
      )}
    </div>
  );
}
