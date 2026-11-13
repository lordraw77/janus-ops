"use client";

import { useEffect, useState } from "react";

import { type Status, StatusBadge } from "@/components/StatusBadge";

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

export default function DashboardPage() {
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

      <p className="text-xs text-fg-muted">
        NOC widgets (host and service hexagons, node cards, unhandled problems) arrive with
        phases F2–F3.
      </p>
    </div>
  );
}
