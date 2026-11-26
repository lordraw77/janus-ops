"use client";

import { BellOff, Check as CheckIcon, CircleX, EyeOff, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { AckDialog, BulkProblemsDialog, CloseProblemDialog, Flags, IgnoreBackupGuestsDialog } from "@/components/Checks";
import { FilterBar } from "@/components/FilterBar";
import { SortTh } from "@/components/SortTh";
import { Alert, Button, Card, Field, Loading, Select, StateBadge, TextInput } from "@/components/ui";
import { ago, formatDuration } from "@/lib/agents";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { InventoryNode } from "@/lib/inventory";
import type { Downtime, Problem } from "@/lib/monitoring";
import { type Getters, useSortedRows } from "@/lib/sort";

type Tab = "open" | "history" | "downtimes";
const POLL_MS = 15_000;

const SEVERITY: Record<string, number> = { crit: 0, unknown: 1, warn: 2, ok: 3 };
const PROBLEM_SORT: Getters<Problem> = {
  state: (p) => SEVERITY[p.state] ?? 9,
  node: (p) => `${p.node_name} / ${p.check_name}`,
  env: (p) => p.environment,
  output: (p) => p.last_output,
  since: (p) => p.opened_at,
  duration: (p) => (p.closed_at ? new Date(p.closed_at).getTime() - new Date(p.opened_at).getTime() : null),
};

function ProblemsTable({ status }: { status: "open" | "closed" }) {
  const { can } = useAuth();
  const manage = can("monitoring.manage");
  const [version, setVersion] = useState(0);
  const [state, setState] = useState<{ data: Problem[] | null; error: string | null }>({ data: null, error: null });
  const [acking, setAcking] = useState<Problem | null>(null);
  const [ignoring, setIgnoring] = useState<Problem | null>(null);
  const [closing, setClosing] = useState<Problem | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulk, setBulk] = useState<"ack" | "close" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [host, setHost] = useState("");
  const [group, setGroup] = useState("");
  const [env, setEnv] = useState("");
  const [level, setLevel] = useState("");
  const nodes = useResource<InventoryNode[]>("/nodes");
  const groupsOf = useMemo(() => new Map((nodes.data ?? []).map((n) => [n.id, n.groups])), [nodes.data]);
  const all = useMemo(() => state.data ?? [], [state.data]);
  const hosts = useMemo(() => [...new Set(all.map((p) => p.node_name))].sort(), [all]);
  const envs = useMemo(() => [...new Set(all.map((p) => p.environment))].sort(), [all]);
  const groupNames = useMemo(() => [...new Set([...groupsOf.values()].flat())].sort(), [groupsOf]);
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter(
      (p) =>
        (!needle || [p.node_name, p.check_name, p.last_output ?? ""].some((v) => v.toLowerCase().includes(needle))) &&
        (!host || p.node_name === host) &&
        (!env || p.environment === env) &&
        (!level || p.state === level) &&
        (!group || (groupsOf.get(p.node_id) ?? []).includes(group)),
    );
  }, [all, q, host, env, level, group, groupsOf]);
  const { sorted, sort, toggle } = useSortedRows(shown, PROBLEM_SORT);
  const selectable = status === "open" && manage;
  // only what is on screen counts: a filter that hides a row also takes it out of the action
  const chosen = useMemo(() => sorted.filter((p) => selected.has(p.id)), [sorted, selected]);
  const allChosen = sorted.length > 0 && chosen.length === sorted.length;
  function pick(id: string, on: boolean) {
    const next = new Set(selected);
    if (on) next.add(id);
    else next.delete(id);
    setSelected(next);
  }

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
  if (!state.data) return <Loading />;
  if (state.data.length === 0)
    return <p className="text-sm text-fg-muted">{status === "open" ? "No open problems." : "No problems yet."}</p>;
  const th = "py-1 pr-3 font-medium";
  return (
    <>
      <FilterBar>
        <Field label="Search">
          <TextInput placeholder="Node, check, output" value={q} onChange={(e) => setQ(e.target.value)} />
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
            {groupNames.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Environment">
          <Select value={env} onChange={(e) => setEnv(e.target.value)}>
            <option value="">All environments</option>
            {envs.map((e) => (
              <option key={e} value={e}>
                {e}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="State">
          <Select value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="">All states</option>
            <option value="crit">Critical</option>
            <option value="warn">Warning</option>
            <option value="unknown">Unknown</option>
          </Select>
        </Field>
      </FilterBar>
      {shown.length === 0 && <p className="mb-2 text-sm text-fg-muted">Nothing matches.</p>}
      {notice && <p className="mb-2 text-xs text-fg-secondary">{notice}</p>}
      {selectable && chosen.length > 0 && (
        <div className="mb-2 flex flex-wrap items-center gap-2 rounded border border-line bg-surface px-3 py-2 text-xs">
          <span className="font-semibold">{chosen.length} selected</span>
          <Button onClick={() => setBulk("ack")}>Acknowledge</Button>
          <Button onClick={() => setBulk("close")}>Close</Button>
          <Button onClick={() => setSelected(new Set())}>Clear selection</Button>
        </div>
      )}
      <table className="w-full text-sm">
        <thead className="text-left text-[11px] uppercase text-fg-muted">
          <tr>
            {selectable && (
              <th className="w-6 py-1 pr-2">
                <input
                  type="checkbox"
                  aria-label="Select all problems shown"
                  checked={allChosen}
                  onChange={(e) => setSelected(e.target.checked ? new Set(sorted.map((p) => p.id)) : new Set())}
                />
              </th>
            )}
            <SortTh label="State" col="state" sort={sort} onSort={toggle} className={th} />
            <SortTh label="Node / check" col="node" sort={sort} onSort={toggle} className={th} />
            <SortTh label="Output" col="output" sort={sort} onSort={toggle} className={th} />
            <SortTh
              label={status === "open" ? "Since" : "Duration"}
              col={status === "open" ? "since" : "duration"}
              sort={sort}
              onSort={toggle}
              className="py-1 pl-3 text-right font-medium"
            />
            {status === "open" && manage && <th />}
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {sorted.map((p) => (
            <tr key={p.id} className="align-top">
              {selectable && (
                <td className="py-2 pr-2">
                  <input
                    type="checkbox"
                    aria-label={`Select ${p.node_name} / ${p.check_name}`}
                    checked={selected.has(p.id)}
                    onChange={(e) => pick(p.id, e.target.checked)}
                  />
                </td>
              )}
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
                {p.closed_at && (
                  <div className={`text-[11px] ${p.closed_by ? "text-warning" : "text-ok"}`}>
                    {p.closed_by
                      ? `Closed by ${p.closed_by}${p.close_comment ? `: ${p.close_comment}` : ""}`
                      : "Recovered"}
                  </div>
                )}
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
                  )}{" "}
                  {p.check_kind === "backup" && (
                    <>
                      <Button onClick={() => setIgnoring(p)} aria-label={`Ignore guests of ${p.check_name}`} title="Some guests are not backed up on purpose">
                        <EyeOff className="h-3 w-3" aria-hidden />
                      </Button>{" "}
                    </>
                  )}
                  <Button onClick={() => setClosing(p)} aria-label={`Close ${p.check_name}`} title="Close the problem">
                    <CircleX className="h-3 w-3" aria-hidden />
                  </Button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {bulk && (
        <BulkProblemsDialog
          action={bulk}
          ids={chosen.map((p) => p.id)}
          onClose={() => setBulk(null)}
          onDone={(r) => {
            setBulk(null);
            setSelected(new Set());
            setNotice(r.skipped > 0 ? `${r.done} done, ${r.skipped} skipped (already closed or out of your scope).` : `${r.done} done.`);
            setVersion((v) => v + 1);
          }}
        />
      )}
      {closing && (
        <CloseProblemDialog
          label={`${closing.node_name} / ${closing.check_name}`}
          problemId={closing.id}
          onClose={() => setClosing(null)}
          onSaved={() => {
            setClosing(null);
            setVersion((v) => v + 1);
          }}
        />
      )}
      {ignoring && (
        <IgnoreBackupGuestsDialog
          label={`${ignoring.node_name} / ${ignoring.check_name}`}
          checkId={ignoring.check_id}
          onClose={() => setIgnoring(null)}
          onSaved={() => {
            setIgnoring(null);
            setVersion((v) => v + 1);
            setTimeout(() => setVersion((v) => v + 1), 3000); // the check runs again at once
          }}
        />
      )}
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
