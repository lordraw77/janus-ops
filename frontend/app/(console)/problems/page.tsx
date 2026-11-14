"use client";

import { BellOff, Check as CheckIcon, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { AckDialog, Flags } from "@/components/Checks";
import { Alert, Button, Card, StateBadge } from "@/components/ui";
import { ago, formatDuration } from "@/lib/agents";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Downtime, Problem } from "@/lib/monitoring";

type Tab = "open" | "history" | "downtimes";
const POLL_MS = 15_000;

function ProblemsTable({ status }: { status: "open" | "closed" }) {
  const { can } = useAuth();
  const manage = can("monitoring.manage");
  const [version, setVersion] = useState(0);
  const [state, setState] = useState<{ data: Problem[] | null; error: string | null }>({ data: null, error: null });
  const [acking, setAcking] = useState<Problem | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      void api<Problem[]>(`/problems?status=${status}&limit=300`).then((r) => {
        if (!cancelled) setState(r.ok ? { data: r.data, error: null } : { data: null, error: message(r.code) });
      });
    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [status, version]);

  async function unack(p: Problem) {
    await api(`/problems/${p.id}/ack`, { method: "DELETE" });
    setVersion((v) => v + 1);
  }

  if (state.error) return <Alert>{state.error}</Alert>;
  if (!state.data) return <p className="text-sm text-fg-muted">Loading…</p>;
  if (state.data.length === 0)
    return <p className="text-sm text-fg-muted">{status === "open" ? "No open problems." : "No problems yet."}</p>;
  return (
    <>
      <table className="w-full text-sm">
        <thead className="text-left text-[11px] uppercase text-fg-muted">
          <tr>
            <th className="py-1 font-medium">State</th>
            <th className="py-1 font-medium">Node / check</th>
            <th className="py-1 font-medium">Output</th>
            <th className="py-1 text-right font-medium">{status === "open" ? "Since" : "Duration"}</th>
            {status === "open" && manage && <th />}
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {state.data.map((p) => (
            <tr key={p.id} className="align-top">
              <td className="py-2 pr-2">
                <StateBadge state={p.state} />
              </td>
              <td className="py-2 pr-3">
                <Link href={`/nodes/?id=${p.node_id}`} className="font-semibold hover:text-primary">
                  {p.node_name}
                </Link>
                <span className="text-fg-secondary"> / {p.check_name}</span>
                <div>
                  <Flags c={p} />
                  {p.notified > 0 && <span className="ml-1 font-mono text-[9px] text-fg-muted">notified ×{p.notified}</span>}
                </div>
                {p.acknowledged_at && (
                  <div className="text-[11px] text-telemetry">
                    {p.acknowledged_by}: {p.ack_comment}
                  </div>
                )}
              </td>
              <td className="max-w-md py-2 pr-3 text-xs text-fg-secondary">{p.last_output}</td>
              <td className="whitespace-nowrap py-2 text-right font-mono text-[11px] text-fg-muted">
                {status === "open"
                  ? ago(p.opened_at)
                  : formatDuration((new Date(p.closed_at!).getTime() - new Date(p.opened_at).getTime()) / 1000)}
                {status === "closed" && <div>{new Date(p.opened_at).toLocaleString()}</div>}
              </td>
              {status === "open" && manage && (
                <td className="py-2 pl-2 text-right">
                  {p.acknowledged_at ? (
                    <Button onClick={() => void unack(p)} aria-label={`Remove acknowledgement of ${p.check_name}`} title="Remove acknowledgement">
                      <X className="h-3 w-3" aria-hidden />
                    </Button>
                  ) : (
                    <Button onClick={() => setAcking(p)} aria-label={`Acknowledge ${p.check_name}`} title="Acknowledge">
                      <CheckIcon className="h-3 w-3" aria-hidden />
                    </Button>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {acking && (
        <AckDialog
          label={`${acking.node_name} / ${acking.check_name}`}
          problemId={acking.id}
          onClose={() => setAcking(null)}
          onSaved={() => {
            setAcking(null);
            setVersion((v) => v + 1);
          }}
        />
      )}
    </>
  );
}

function Downtimes() {
  const { can } = useAuth();
  const list = useResource<Downtime[]>("/downtimes");
  const [error, setError] = useState<string | null>(null);

  async function end(d: Downtime) {
    if (!window.confirm(`End the downtime of ${d.target_name} now?`)) return;
    const r = await api(`/downtimes/${d.id}`, { method: "DELETE" });
    setError(r.ok ? null : message(r.code));
    list.reload();
  }

  return (
    <>
      {(error || list.error) && <Alert>{error ?? list.error}</Alert>}
      <p className="mb-2 text-xs text-fg-muted">
        Schedule a downtime from a node page (whole node) or here for what is listed. During a downtime problems are
        recorded but not notified.
      </p>
      {list.data?.length === 0 && <p className="text-sm text-fg-muted">No current or future downtimes.</p>}
      <div className="divide-y divide-line">
        {(list.data ?? []).map((d) => (
          <div key={d.id} className="flex items-center gap-3 py-2 text-sm">
            <span
              className={`rounded border px-1.5 font-mono text-[10px] ${d.active ? "border-telemetry/40 text-telemetry" : "border-line text-fg-muted"}`}
            >
              {d.active ? "ACTIVE" : "SCHEDULED"}
            </span>
            <div className="min-w-0 flex-1">
              <div>
                <span className="text-[11px] uppercase text-fg-muted">{d.scope}</span> {d.target_name}
              </div>
              <div className="text-xs text-fg-secondary">{d.reason}</div>
            </div>
            <div className="text-right font-mono text-[11px] text-fg-muted">
              {new Date(d.starts_at).toLocaleString()} → {new Date(d.ends_at).toLocaleString()}
              <div>{d.created_by}</div>
            </div>
            {can("monitoring.manage") && (
              <Button onClick={() => void end(d)} aria-label={`End downtime of ${d.target_name}`}>
                <BellOff className="h-3 w-3" aria-hidden /> End
              </Button>
            )}
          </div>
        ))}
      </div>
    </>
  );
}

export default function ProblemsPage() {
  const [tab, setTab] = useState<Tab>("open");
  const tabs: [Tab, string][] = [
    ["open", "Open"],
    ["history", "History"],
    ["downtimes", "Downtimes"],
  ];
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Problems</h1>
      <div className="flex gap-1 border-b border-line" role="tablist">
        {tabs.map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            onClick={() => setTab(key)}
            className={`-mb-px border-b-2 px-3 py-1.5 text-xs font-semibold ${tab === key ? "border-primary text-primary" : "border-transparent text-fg-muted hover:text-fg"}`}
          >
            {label}
          </button>
        ))}
      </div>
      <Card>
        {tab === "open" && <ProblemsTable status="open" />}
        {tab === "history" && <ProblemsTable status="closed" />}
        {tab === "downtimes" && <Downtimes />}
      </Card>
    </div>
  );
}
