"use client";

import Link from "next/link";
import { type DragEvent, useEffect, useMemo, useState } from "react";

import { AttentionTable, CardControls, HexagonHealth, NodeCard } from "@/components/Noc";
import { StatusBadge } from "@/components/StatusBadge";
import { Alert } from "@/components/ui";
import type { Dashboard } from "@/lib/agents";
import { api, message } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { DEFAULT_LAYOUT, type Layout, moveBefore, readLayout, sortNodes } from "@/lib/dashboardLayout";
import { useEnvironments } from "@/lib/environments";

const POLL_MS = 10_000;
const LAYOUT_KEY = "janus.dashboard.layout";

/** The card layout, kept in this browser (it is a personal view, not a setting of the installation). */
function useLayout(): [Layout, (next: Layout) => void] {
  // only mounted after sign-in, in the browser: reading the storage while creating the state is safe
  const [layout, setLayout] = useState<Layout>(() => {
    try {
      return readLayout(window.localStorage.getItem(LAYOUT_KEY));
    } catch {
      return DEFAULT_LAYOUT; // storage blocked
    }
  });
  const save = (next: Layout) => {
    setLayout(next);
    try {
      window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(next));
    } catch {
      /* not saved, still applied */
    }
  };
  return [layout, save];
}

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
  const { names } = useEnvironments();
  const [layout, setLayout] = useLayout();
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const envOrder = (env: string) => names.indexOf(env);
  const nodes = useMemo(
    () => (result?.data ? sortNodes(result.data.nodes, layout, envOrder) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- envOrder only reads `names`
    [result, layout, names],
  );
  if (result === null)
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading dashboard">
        <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_1fr]">
          <div className="h-52 animate-pulse rounded-lg border border-line bg-surface" />
          <div className="h-52 animate-pulse rounded-lg border border-line bg-surface" />
        </div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4 2xl:grid-cols-5">
          {Array.from({ length: 5 }, (_, i) => (
            <div key={i} className="h-56 animate-pulse rounded-lg border border-line bg-surface" />
          ))}
        </div>
      </div>
    );
  if (result.error || !result.data) return <Alert>{result.error}</Alert>;
  const dash = result.data;
  const free = layout.mode === "custom";
  const drop = (target: string | null) => {
    if (dragging) setLayout({ ...layout, order: moveBefore(nodes.map((n) => n.node_id), dragging, target) });
    setDragging(null);
    setOver(null);
  };
  return (
    <>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_1fr]">
        <HexagonHealth hosts={dash.hosts} services={dash.services} controls={<CardControls layout={layout} onChange={setLayout} />} />
        <AttentionTable items={dash.attention} handled={dash.handled} />
      </div>
      {nodes.length === 0 ? (
        <p className="text-sm text-fg-muted">
          No nodes yet. Add them in the <Link href="/inventory/" className="text-primary">inventory</Link>,
          enable the Janus agent capability and install it from the node page.
        </p>
      ) : (
        <div className={`grid items-start gap-3 sm:grid-cols-2 ${layout.compact ? "xl:grid-cols-3 2xl:grid-cols-4" : "xl:grid-cols-4 2xl:grid-cols-5"}`}>
          {nodes.map((n) => (
            <div
              key={n.node_id}
              onDragOver={free && dragging ? (e: DragEvent) => { e.preventDefault(); setOver(n.node_id); } : undefined}
              onDrop={free && dragging ? (e: DragEvent) => { e.preventDefault(); drop(n.node_id); } : undefined}
              className={`rounded-lg transition-opacity ${dragging === n.node_id ? "opacity-40" : ""} ${over === n.node_id && dragging !== n.node_id ? "ring-2 ring-primary" : ""}`}
            >
              <NodeCard
                node={n}
                compact={layout.compact}
                grip={
                  free
                    ? {
                        onDragStart: (e) => {
                          e.dataTransfer.effectAllowed = "move";
                          e.dataTransfer.setData("text/plain", n.node_id);
                          const card = e.currentTarget.closest("article");
                          if (card) e.dataTransfer.setDragImage(card, 16, 16);
                          setDragging(n.node_id);
                        },
                        onDragEnd: () => {
                          setDragging(null);
                          setOver(null);
                        },
                      }
                    : undefined
                }
              />
            </div>
          ))}
          {free && dragging && (
            <div
              onDragOver={(e) => { e.preventDefault(); setOver("__end"); }}
              onDrop={(e) => { e.preventDefault(); drop(null); }}
              className={`flex min-h-16 items-center justify-center rounded-lg border-2 border-dashed text-xs text-fg-muted ${over === "__end" ? "border-primary text-primary" : "border-line"}`}
            >
              Drop here to put it last
            </div>
          )}
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
