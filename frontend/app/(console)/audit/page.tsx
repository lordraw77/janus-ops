"use client";

import { ShieldAlert, ShieldCheck } from "lucide-react";
import { type FormEvent, useCallback, useEffect, useState } from "react";

import { FilterBar } from "@/components/FilterBar";
import { SortTh } from "@/components/SortTh";

import { Alert, Button, Card, Field, Select, TextInput } from "@/components/ui";
import { api, message } from "@/lib/api";
import { RequirePermission } from "@/lib/auth";
import type { Group, InventoryNode } from "@/lib/inventory";
import { type Getters, useSortedRows } from "@/lib/sort";

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

const EVENT_SORT: Getters<Event> = {
  id: (e) => e.id,
  time: (e) => e.ts,
  actor: (e) => e.actor_name,
  action: (e) => e.action,
  target: (e) => (e.target_id ? `${e.target_type}:${e.target_id}` : null),
  source: (e) => e.source_ip,
  details: (e) => (Object.keys(e.payload).length ? JSON.stringify(e.payload) : null),
};

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
  const [host, setHost] = useState("");
  const [group, setGroup] = useState("");
  const [hosts, setHosts] = useState<string[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [error, setError] = useState<string | null>(null);
  const { sorted, sort, toggle } = useSortedRows(events, EVENT_SORT);

  const load = useCallback(
    async (before?: number) => {
      const params = new URLSearchParams({ limit: "50" });
      if (before) params.set("before_id", String(before));
      if (action.trim()) params.set("action", action.trim());
      if (actor.trim()) params.set("actor", actor.trim());
      if (host) params.set("host", host);
      if (group) params.set("group", group);
      const r = await api<Page>(`/audit?${params}`);
      if (!r.ok) return setError(message(r.code));
      setError(null);
      setEvents((prev) => (before ? [...prev, ...r.data.events] : r.data.events));
      setNext(r.data.next_before_id);
    },
    [action, actor, host, group],
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
    // the choices for the host and group filters (an auditor without them just gets no list)
    void api<InventoryNode[]>("/nodes").then((r) => {
      if (!cancelled && r.ok) setHosts(r.data.map((n) => n.name).sort());
    });
    void api<Group[]>("/groups").then((r) => {
      if (!cancelled && r.ok) setGroups(r.data);
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
    <div className="space-y-4">
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
        <form onSubmit={filter}>
          <FilterBar>
            <Field label="Action">
              <TextInput placeholder="Prefix, e.g. auth.login" value={action} onChange={(e) => setAction(e.target.value)} />
            </Field>
            <Field label="Actor">
              <TextInput placeholder="User name" value={actor} onChange={(e) => setActor(e.target.value)} />
            </Field>
            <Field label="Host">
              <Select value={host} onChange={(e) => setHost(e.target.value)}>
                <option value="">All hosts</option>
                {hosts.map((h) => (
                  <option key={h} value={h}>
                    {h}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Group">
              <Select value={group} onChange={(e) => setGroup(e.target.value)}>
                <option value="">All groups</option>
                {groups.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Button type="submit" variant="primary" className="h-[34px] px-5">
              Filter
            </Button>
          </FilterBar>
        </form>
        {error && <Alert>{error}</Alert>}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-line text-fg-muted">
              <tr>
                <SortTh label="#" col="id" sort={sort} onSort={toggle} />
                <SortTh label="Time" col="time" sort={sort} onSort={toggle} />
                <SortTh label="Actor" col="actor" sort={sort} onSort={toggle} />
                <SortTh label="Action" col="action" sort={sort} onSort={toggle} />
                <SortTh label="Target" col="target" sort={sort} onSort={toggle} />
                <SortTh label="Source" col="source" sort={sort} onSort={toggle} />
                <SortTh label="Details" col="details" sort={sort} onSort={toggle} className="py-2 font-medium" />
              </tr>
            </thead>
            <tbody className="divide-y divide-line font-mono">
              {sorted.map((e) => (
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
