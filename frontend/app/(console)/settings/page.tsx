"use client";

import { useEffect, useState } from "react";

import { AiHealthCard, AiProvidersCard, AiRoutesCard } from "@/components/AiProviders";
import { AiSettingsCard, McpSettingsCard } from "@/components/AiSettings";
import { EnvironmentsCard } from "@/components/Environments";
import { Alert, Button, Card } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { RequirePermission, useAuth } from "@/lib/auth";
import type { InventoryNode } from "@/lib/inventory";
import { isOlder } from "@/lib/version";

type Ready = { status: string; version: string; checks: Record<string, string> };

const CHECK_LABELS: Record<string, string> = {
  database: "Database",
  timescaledb: "TimescaleDB",
  schema: "Schema",
  secret_manager: "Secret manager",
};

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 border-t border-line py-2 text-xs first:border-t-0">
      <span className="text-fg-muted">{label}</span>
      <span className="font-mono">{children}</span>
    </div>
  );
}

function AgentsCard() {
  const { can } = useAuth();
  const health = useResource<{ version: string }>("/health");
  const nodes = useResource<InventoryNode[]>("/nodes");
  const [rollout, setRollout] = useState<string | null>(null);
  const target = health.data?.version ?? null;
  const agents = (nodes.data ?? []).filter((n) => n.agent && !n.agent.revoked);
  const outdated = agents.filter((n) => isOlder(n.agent?.version, target));
  const byVersion = new Map<string, number>();
  for (const n of agents) {
    const v = n.agent?.version ?? "unknown";
    byVersion.set(v, (byVersion.get(v) ?? 0) + 1);
  }

  async function updateFleet() {
    if (!window.confirm("Push the signed agent update to every connected agent that is not on the signed version?")) return;
    setRollout("Updating…");
    const r = await api<{ node_name: string; result: string; detail: string | null }[]>("/agents/update-all", {
      method: "POST",
      body: {},
    });
    if (!r.ok) return setRollout(message(r.code));
    const count = (k: string) => r.data.filter((x) => x.result === k).length;
    setRollout(`Updated ${count("updated")}, already current or offline ${count("skipped")}, failed ${count("failed")}.`);
    nodes.reload();
  }

  return (
    <Card
      title="Agents"
      actions={
        can("agents.manage") ? (
          <Button variant="primary" disabled={outdated.length === 0} onClick={() => void updateFleet()}>
            {outdated.length ? `Update ${outdated.length} to ${target}` : "All up to date"}
          </Button>
        ) : null
      }
    >
      {nodes.error && <Alert>{nodes.error}</Alert>}
      <Row label="Signed version (this server)">{target ?? "…"}</Row>
      <Row label="Agents installed">{agents.length}</Row>
      {[...byVersion.entries()].sort().map(([v, n]) => (
        <Row key={v} label={`On ${v}`}>
          <span className={isOlder(v, target) ? "text-warn" : "text-ok"}>{n}</span>
        </Row>
      ))}
      {rollout && <p className="mt-2 text-xs text-fg-secondary">{rollout}</p>}
    </Card>
  );
}

function SystemCard() {
  const ready = useResource<Ready>("/ready");
  const data = ready.data;
  return (
    <Card title="System">
      {ready.error && <Alert>{ready.error}</Alert>}
      <Row label="Version">{data?.version ?? "…"}</Row>
      <Row label="Status">
        <span className={data?.status === "ok" ? "text-ok" : "text-crit"}>{data?.status ?? "…"}</span>
      </Row>
      {Object.entries(data?.checks ?? {}).map(([k, v]) => (
        <Row key={k} label={CHECK_LABELS[k] ?? k}>
          <span className={v === "ok" ? "text-ok" : "text-crit"}>{v}</span>
        </Row>
      ))}
    </Card>
  );
}

type TabId = "general" | "providers" | "routes" | "models";

const TABS: { id: TabId; label: string; permission?: "ai.manage" }[] = [
  { id: "general", label: "General" },
  { id: "providers", label: "AI providers", permission: "ai.manage" },
  { id: "routes", label: "Routes & health", permission: "ai.manage" },
  { id: "models", label: "AI models & tools", permission: "ai.manage" },
];

function Settings() {
  const { can } = useAuth();
  const tabs = TABS.filter((t) => !t.permission || can(t.permission));
  const [tab, setTab] = useState<TabId>("general");

  // the tab lives in the URL hash, so a reload or a shared link opens the same one
  useEffect(() => {
    const fromHash = window.location.hash.replace("#", "") as TabId;
    if (tabs.some((t) => t.id === fromHash)) setTab(fromHash); // eslint-disable-line react-hooks/set-state-in-effect
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function open(id: TabId) {
    setTab(id);
    window.history.replaceState(null, "", `#${id}`);
  }

  const current = tabs.some((t) => t.id === tab) ? tab : "general";
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Settings</h1>
      <div className="flex flex-wrap gap-1 border-b border-line" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={current === t.id}
            onClick={() => open(t.id)}
            className={`-mb-px border-b-2 px-3 py-1.5 text-xs font-semibold ${current === t.id ? "border-primary text-primary" : "border-transparent text-fg-muted hover:text-fg"}`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {current === "general" && (
        <div className="space-y-4">
          {can("environments.manage") && <EnvironmentsCard />}
          <div className="grid gap-4 lg:grid-cols-2">
            <AgentsCard />
            <SystemCard />
          </div>
        </div>
      )}
      {current === "providers" && <AiProvidersCard />}
      {current === "routes" && (
        <div className="space-y-4">
          <AiRoutesCard />
          <AiHealthCard />
        </div>
      )}
      {current === "models" && (
        <div className="space-y-4">
          <AiSettingsCard />
          <McpSettingsCard />
        </div>
      )}
    </div>
  );
}

export default function SettingsPage() {
  return (
    <RequirePermission permission="inventory.read">
      <Settings />
    </RequirePermission>
  );
}
