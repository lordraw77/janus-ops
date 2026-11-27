"use client";

import { BellPlus, RadioTower, Trash2, Upload } from "lucide-react";
import { type ChangeEvent, useEffect, useState } from "react";

import { Alert, Button, Card, Field, Loading, Modal, Select, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { RequirePermission, useAuth } from "@/lib/auth";

type Varbind = { oid: string; value: string; name: string | null };
type Trap = {
  id: number;
  received_at: string;
  node_id: string;
  node_name: string;
  source_ip: string;
  version: string;
  trap_oid: string | null;
  trap_name: string | null;
  varbinds: Varbind[];
};
type Status = { enabled: boolean; port: number | null; dropped: number; last_hour: number };
type Mib = { module: string; filename: string; uploaded_at: string; objects: number; unresolved: string[] };

const POLL_MS = 10_000;

/** A check that raises a problem when this node sends traps (probe snmp_traps.recent). */
function AlertDialog({ trap, onClose }: { trap: Trap; onClose: () => void }) {
  const label = trap.trap_name ?? trap.trap_oid ?? "trap";
  const [scope, setScope] = useState<"type" | "any">(trap.trap_oid ? "type" : "any");
  const [minutes, setMinutes] = useState("15");
  const [severity, setSeverity] = useState<"crit" | "warn">("crit");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const name = scope === "type" ? `Trap ${label}` : "Any SNMP trap";

  async function create() {
    setBusy(true);
    setError(null);
    const args: Record<string, string> = { minutes: minutes.trim() };
    if (scope === "type" && trap.trap_oid) args.oid = trap.trap_oid;
    const r = await api("/checks", {
      method: "POST",
      body: {
        node_id: trap.node_id,
        name,
        kind: "probe",
        params: { probe: "snmp_traps.recent", args },
        comparison: "gt",
        [severity === "crit" ? "crit_threshold" : "warn_threshold"]: 0,
        check_cycles: 1,
        interval_s: 60,
        enabled: true,
      },
    });
    setBusy(false);
    if (!r.ok) return setError(r.code === "check_exists" ? "This node already has a check with this name." : message(r.code));
    setDone(true);
  }

  return (
    <Modal title={`Alert on traps from ${trap.node_name}`} onClose={onClose}>
      {done ? (
        <div className="space-y-3">
          <Alert kind="ok">Check “{name}” created on {trap.node_name}. It runs every minute.</Alert>
          <div className="flex justify-end">
            <Button type="button" onClick={onClose}>
              Close
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          <Field label="Which traps">
            <Select value={scope} onChange={(e) => setScope(e.target.value as "type" | "any")}>
              {trap.trap_oid && <option value="type">Only this one: {label}</option>}
              <option value="any">Any trap from this node</option>
            </Select>
          </Field>
          <Field label="Stays a problem for (minutes)" hint="The problem closes by itself after this long without a new trap.">
            <TextInput type="number" min={1} max={1440} value={minutes} onChange={(e) => setMinutes(e.target.value)} />
          </Field>
          <Field label="Severity">
            <Select value={severity} onChange={(e) => setSeverity(e.target.value as "crit" | "warn")}>
              <option value="crit">Critical</option>
              <option value="warn">Warning</option>
            </Select>
          </Field>
          {error && <Alert>{error}</Alert>}
          <div className="flex justify-end gap-2">
            <Button type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="button" variant="primary" disabled={busy || !/^\d+$/.test(minutes.trim())} onClick={() => void create()}>
              {busy ? "Creating…" : "Create the check"}
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
const MIB_ERRORS: Record<string, string> = {
  not_a_mib: "This file is not a MIB (no “… DEFINITIONS ::= BEGIN” found).",
  mib_empty: "No named OIDs were found in this file.",
  mib_too_large: "The file is larger than 2 MiB.",
};

function Receiver({ status }: { status: Status | null }) {
  if (!status) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1 rounded-lg border border-line bg-surface px-4 py-2.5 text-xs">
      <span className="font-semibold uppercase tracking-wider text-fg-muted">Receiver</span>
      {status.enabled ? (
        <>
          <span className="flex items-center gap-2 text-ok">
            <span className="inline-block h-2 w-2 rounded-full bg-ok" aria-hidden />
            listening on UDP {status.port} (v2c)
          </span>
          <span className="font-mono text-fg-muted">{status.last_hour} in the last hour</span>
          {status.dropped > 0 && <span className="font-mono text-warn">{status.dropped} dropped since start</span>}
        </>
      ) : (
        <span className="text-fg-muted">
          off. Turn on the <span className="font-mono">snmp-traps</span> profile (<span className="font-mono">COMPOSE_PROFILES=snmp-traps</span>) and set{" "}
          <span className="font-mono">JANUS_SNMP_TRAP_COMMUNITIES</span>.
        </span>
      )}
    </div>
  );
}

function Traps() {
  const { can } = useAuth();
  const [alerting, setAlerting] = useState<Trap | null>(null);
  const [traps, setTraps] = useState<Trap[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const status = useResource<Status>("/snmp-traps/status");

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      void api<Trap[]>("/snmp-traps?limit=200").then((r) => {
        if (cancelled) return;
        if (r.ok) {
          setTraps(r.data);
          setError(null);
        } else setError(message(r.code));
      });
    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <Card title="Traps received">
      <div className="mb-3">
        <Receiver status={status.data} />
      </div>
      {error && <Alert>{error}</Alert>}
      {traps === null && !error && <Loading />}
      {traps?.length === 0 && (
        <p className="text-sm text-fg-muted">
          No trap yet. Point a device&apos;s trap target at this host (UDP 162) with the community set on the server; the
          device must be in the inventory with the same address (no SNMP credentials needed).
        </p>
      )}
      {traps && traps.length > 0 && (
        <table className="w-full text-left text-sm">
          <thead className="text-[11px] uppercase text-fg-muted">
            <tr>
              <th className="py-1 pr-3 font-medium">Time</th>
              <th className="py-1 pr-3 font-medium">Node</th>
              <th className="py-1 pr-3 font-medium">Trap</th>
              <th className="py-1 pr-3 font-medium">Values</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {traps.map((t) => {
              const payload = t.varbinds.filter((v) => !v.oid.startsWith("1.3.6.1.2.1.1.3.") && !v.oid.startsWith("1.3.6.1.6.3.1.1.4.1."));
              return (
                <tr key={t.id} className="align-top transition-colors hover:bg-elevated/50">
                  <td className="whitespace-nowrap py-1.5 pr-3 font-mono text-[11px] text-fg-muted">
                    {new Date(t.received_at).toLocaleString()}
                  </td>
                  <td className="py-1.5 pr-3">
                    <div>{t.node_name}</div>
                    <div className="font-mono text-[11px] text-fg-muted">{t.source_ip}</div>
                  </td>
                  <td className="py-1.5 pr-3">
                    <div>{t.trap_name ?? "unnamed trap"}</div>
                    <div className="font-mono text-[11px] text-fg-muted">{t.trap_oid}</div>
                  </td>
                  <td className="py-1.5 pr-3 text-xs">
                    {payload.slice(0, open === t.id ? undefined : 2).map((v) => (
                      <div key={v.oid} className="flex gap-2">
                        <span className="text-fg-muted" title={v.oid}>
                          {v.name ?? v.oid}
                        </span>
                        <span className="min-w-0 truncate font-mono">{v.value}</span>
                      </div>
                    ))}
                    {payload.length > 2 && (
                      <button
                        type="button"
                        onClick={() => setOpen(open === t.id ? null : t.id)}
                        className="text-[11px] text-primary hover:underline"
                      >
                        {open === t.id ? "less" : `+${payload.length - 2} more`}
                      </button>
                    )}
                  </td>
                  <td className="py-1.5 text-right">
                    {can("monitoring.manage") && (
                      <button
                        type="button"
                        onClick={() => setAlerting(t)}
                        title="Raise a problem when this happens"
                        className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11px] text-fg-secondary transition-colors hover:border-line-strong hover:text-fg"
                      >
                        <BellPlus className="h-3 w-3" aria-hidden /> Alert
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {alerting && <AlertDialog trap={alerting} onClose={() => setAlerting(null)} />}
    </Card>
  );
}

function Mibs() {
  const { can } = useAuth();
  const mibs = useResource<Mib[]>("/snmp-mibs");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);
  const manage = can("monitoring.manage");

  async function upload(e: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    setBusy(true);
    setError(null);
    setReport(null);
    const lines: string[] = [];
    const failed: string[] = [];
    // dependencies first is not required: a name another file defines is read from the ones stored,
    // so a file that needs one not loaded yet reports it as unresolved and can be sent again
    for (const file of files) {
      const r = await api<{ module: string; objects: number; unresolved: string[] }>("/snmp-mibs", {
        method: "POST",
        body: { filename: file.name, content: await file.text() },
      });
      if (r.ok) {
        lines.push(
          `${r.data.module}: ${r.data.objects} names` +
            (r.data.unresolved.length ? ` (${r.data.unresolved.length} unresolved: load the MIB they hang from, then this one again)` : ""),
        );
      } else failed.push(`${file.name}: ${MIB_ERRORS[r.code] ?? message(r.code)}`);
    }
    setBusy(false);
    if (lines.length) setReport(lines.join("\n"));
    if (failed.length) setError(failed.join("\n"));
    mibs.reload();
  }

  async function remove(module: string) {
    setError(null);
    const r = await api(`/snmp-mibs/${encodeURIComponent(module)}`, { method: "DELETE" });
    if (!r.ok) return setError(message(r.code));
    mibs.reload();
  }

  return (
    <Card
      title="MIB files"
      actions={
        manage && (
          <label
            className={`inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-line bg-elevated px-3 py-1.5 text-xs font-semibold transition-colors hover:border-line-strong focus-within:outline-2 ${busy ? "pointer-events-none opacity-50" : ""}`}
          >
            <Upload className="h-3.5 w-3.5" aria-hidden /> Upload MIB
            <input type="file" multiple accept=".mib,.txt,.my,text/plain" className="sr-only" onChange={(e) => void upload(e)} />
          </label>
        )
      }
    >
      <p className="mb-3 text-xs text-fg-muted">
        A MIB gives names to numeric OIDs in the trap list. Only names and OIDs are kept, not the file. Checks keep
        using numeric OIDs, so a MIB is never needed for monitoring.
      </p>
      {error && <Alert>{error}</Alert>}
      {report && <Alert kind="ok">{report}</Alert>}
      {mibs.error && <Alert>{mibs.error}</Alert>}
      {mibs.data && mibs.data.length === 0 && <p className="text-sm text-fg-muted">No MIB loaded. Standard names (system, interfaces, linkUp/linkDown…) are built in.</p>}
      {mibs.data && mibs.data.length > 0 && (
        <ul className="divide-y divide-line text-sm">
          {mibs.data.map((m) => (
            <li key={m.module} className="flex items-center justify-between gap-3 py-1.5">
              <span className="min-w-0">
                <span className="font-medium">{m.module}</span>
                <span className="ml-2 font-mono text-[11px] text-fg-muted">
                  {m.objects} names · {m.filename || "no file name"}
                </span>
                {m.unresolved.length > 0 && (
                  <span className="ml-2 text-[11px] text-warn" title={m.unresolved.join(", ")}>
                    {m.unresolved.length} unresolved
                  </span>
                )}
              </span>
              {manage && (
                <Button type="button" variant="danger" aria-label={`Remove ${m.module}`} onClick={() => void remove(m.module)}>
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export default function TrapsPage() {
  return (
    <RequirePermission permission="inventory.read">
      <div className="space-y-4">
        <h1 className="flex items-center gap-2 text-lg font-semibold">
          <RadioTower className="h-5 w-5 text-telemetry" aria-hidden /> SNMP traps
        </h1>
        <Traps />
        <Mibs />
      </div>
    </RequirePermission>
  );
}
