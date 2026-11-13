"use client";

import { ShieldAlert, ShieldCheck } from "lucide-react";
import { type FormEvent, useCallback, useEffect, useState } from "react";

import { Alert, Button, Card, TextInput } from "@/components/ui";
import { api, message } from "@/lib/api";
import { RequirePermission } from "@/lib/auth";

type Event = {
  id: number;
  ts: string;
  actor_name: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  source_ip: string | null;
  payload: Record<string, unknown>;
};
type Page = { events: Event[]; next_before_id: number | null };
type Chain = { intact: boolean; first_broken_id: number | null; events: number };

function actionColor(action: string): string {
  if (action.endsWith("_failed") || action.includes("deleted")) return "text-crit";
  if (action.startsWith("auth.breakglass") || action.includes("reset")) return "text-warn";
  if (action.startsWith("roles.") || action.startsWith("users.")) return "text-telemetry";
  return "text-fg";
}

function AuditLog() {
  const [events, setEvents] = useState<Event[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [chain, setChain] = useState<Chain | null>(null);
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (before?: number) => {
      const params = new URLSearchParams({ limit: "50" });
      if (before) params.set("before_id", String(before));
      if (action.trim()) params.set("action", action.trim());
      if (actor.trim()) params.set("actor", actor.trim());
      const r = await api<Page>(`/audit?${params}`);
      if (!r.ok) return setError(message(r.code));
      setError(null);
      setEvents((prev) => (before ? [...prev, ...r.data.events] : r.data.events));
      setNext(r.data.next_before_id);
    },
    [action, actor],
  );

  useEffect(() => {
    let cancelled = false;
    void api<Page>("/audit?limit=50").then((r) => {
      if (cancelled) return;
      if (!r.ok) return setError(message(r.code));
      setEvents(r.data.events);
      setNext(r.data.next_before_id);
    });
    void api<Chain>("/audit/verify").then((r) => {
      if (!cancelled && r.ok) setChain(r.data);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  function filter(e: FormEvent) {
    e.preventDefault();
    void load();
  }

  return (
    <div className="max-w-6xl space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Audit log</h1>
        {chain &&
          (chain.intact ? (
            <span className="flex items-center gap-1.5 text-xs text-ok">
              <ShieldCheck className="h-4 w-4" aria-hidden /> Hash chain intact · {chain.events} events
            </span>
          ) : (
            <span className="flex items-center gap-1.5 text-xs text-crit">
              <ShieldAlert className="h-4 w-4" aria-hidden /> Chain broken at event #{chain.first_broken_id}
            </span>
          ))}
      </div>
      <Card>
        <form onSubmit={filter} className="mb-4 flex flex-wrap items-end gap-2">
          <TextInput placeholder="Action prefix, e.g. auth.login" value={action} onChange={(e) => setAction(e.target.value)} className="max-w-xs" />
          <TextInput placeholder="Actor" value={actor} onChange={(e) => setActor(e.target.value)} className="max-w-[12rem]" />
          <Button type="submit" variant="primary">
            Filter
          </Button>
        </form>
        {error && <Alert>{error}</Alert>}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-line text-fg-muted">
              <tr>
                <th className="py-2 pr-3 font-medium">#</th>
                <th className="py-2 pr-3 font-medium">Time</th>
                <th className="py-2 pr-3 font-medium">Actor</th>
                <th className="py-2 pr-3 font-medium">Action</th>
                <th className="py-2 pr-3 font-medium">Target</th>
                <th className="py-2 pr-3 font-medium">Source</th>
                <th className="py-2 font-medium">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line font-mono">
              {events.map((e) => (
                <tr key={e.id}>
                  <td className="py-1.5 pr-3 text-fg-muted">{e.id}</td>
                  <td className="whitespace-nowrap py-1.5 pr-3 text-fg-secondary">{new Date(e.ts).toLocaleString()}</td>
                  <td className="py-1.5 pr-3">{e.actor_name}</td>
                  <td className={`py-1.5 pr-3 ${actionColor(e.action)}`}>{e.action}</td>
                  <td className="py-1.5 pr-3">{e.target_id ? `${e.target_type}:${e.target_id}` : ""}</td>
                  <td className="py-1.5 pr-3 text-fg-muted">{e.source_ip ?? ""}</td>
                  <td className="max-w-md truncate py-1.5 text-fg-muted" title={JSON.stringify(e.payload)}>
                    {Object.keys(e.payload).length ? JSON.stringify(e.payload) : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {next && (
          <div className="mt-3">
            <Button onClick={() => void load(next)}>Load older</Button>
          </div>
        )}
      </Card>
    </div>
  );
}

export default function AuditPage() {
  return (
    <RequirePermission permission="audit.read">
      <AuditLog />
    </RequirePermission>
  );
}
