"use client";

import { Pencil, Play, Plus, Radar, Server, Trash2 } from "lucide-react";
import Link from "next/link";
import { type FormEvent, useEffect, useMemo, useState } from "react";

import { LineChart } from "@/components/LineChart";
import { Alert, Button, Card, Field, Loading, Modal, Select, Spinner, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  KIND_LABEL,
  type LanEvent,
  type LanEventKind,
  type LanHistory,
  type LanHost,
  type LanScanner,
  type LanSubnet,
  bars,
  displayName,
  matches,
  prevLabel,
  span,
} from "@/lib/lan";

const POLL_MS = 30_000;
const RANGES: [string, string][] = [
  ["24", "Last 24 hours"],
  ["168", "Last 7 days"],
  ["720", "Last 30 days"],
  ["2160", "Last 90 days"],
];
const INTERVALS = [1, 2, 5, 10, 15, 30, 60, 360, 1440];
const KIND_STYLE: Record<LanEventKind, string> = {
  new: "bg-telemetry/15 text-telemetry",
  online: "bg-ok/15 text-ok",
  offline: "bg-offline/30 text-fg-secondary",
  mac_changed: "bg-warn/15 text-warn",
  ip_changed: "bg-warn/15 text-warn",
};

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");
const ago = (iso: string) => span(Date.now() - Date.parse(iso));
const intervalLabel = (m: number) => (m < 60 ? `${m} min` : m < 1440 ? `${m / 60} h` : "1 day");

/** Reloads a resource on a timer: the page watches a network, so it should keep up with it. */
function usePolling(reload: () => void, ms = POLL_MS) {
  useEffect(() => {
    const timer = setInterval(reload, ms);
    return () => clearInterval(timer);
  }, [reload, ms]);
}

function Dot({ online }: { online: boolean }) {
  return (
    <span
      className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full ${online ? "bg-ok" : "bg-offline"}`}
      role="img"
      aria-label={online ? "online" : "offline"}
    />
  );
}

/** Marks an address that is also a node of the inventory, with a link to it. */
function InventoryBadge({ host }: { host: LanHost }) {
  if (!host.node_id) return null;
  return (
    <Link
      href={`/nodes/?id=${host.node_id}`}
      onClick={(e) => e.stopPropagation()}
      title={`In the inventory as ${host.node_name}`}
      className="inline-flex items-center gap-1 rounded bg-primary/15 px-1.5 py-0.5 text-[11px] font-medium text-primary hover:bg-primary/25"
    >
      <Server className="h-3 w-3" aria-hidden /> {host.node_name}
    </Link>
  );
}

function KindBadge({ kind }: { kind: LanEventKind }) {
  return <span className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${KIND_STYLE[kind]}`}>{KIND_LABEL[kind]}</span>;
}

// ---------------------------------------------------------------- activity chart
function Activity({ subnetId }: { subnetId: string }) {
  const [hours, setHours] = useState("24");
  const query = `/lan/activity?hours=${hours}${subnetId ? `&subnet_id=${subnetId}` : ""}`;
  const points = useResource<{ t: number; v: number }[]>(query);
  usePolling(points.reload);
  const bucketS = Math.max(60, Math.floor((Number(hours) * 3600) / 200));
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-fg-muted">Hosts up over time</h3>
        <Select value={hours} onChange={(e) => setHours(e.target.value)} className="!w-auto" aria-label="Period">
          {RANGES.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </Select>
      </div>
      {points.error && <Alert>{points.error}</Alert>}
      <LineChart
        title="Hosts up after each sweep"
        series={[{ key: "up", label: "Hosts up", points: points.data ?? [] }]}
        unit="count"
        bucketS={bucketS}
      />
    </div>
  );
}

// ---------------------------------------------------------------- one host
function HostDetail({ host, manage, onClose, onChanged }: { host: LanHost; manage: boolean; onClose: () => void; onChanged: () => void }) {
  const [hours, setHours] = useState("168");
  const history = useResource<LanHistory>(`/lan/hosts/${host.id}/history?hours=${hours}`);
  const [label, setLabel] = useState(host.label ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const h = history.data;
  const strip = useMemo(() => (h ? bars(h.segments, h.start, h.end) : []), [h]);

  async function saveLabel(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const r = await api(`/lan/hosts/${host.id}`, { method: "PATCH", body: { label: label.trim() || null } });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    onChanged();
  }

  async function forget() {
    if (!window.confirm(`Forget ${host.ip} (${host.mac}) and its history? It comes back if a sweep sees it again.`)) return;
    const r = await api(`/lan/hosts/${host.id}`, { method: "DELETE" });
    if (!r.ok) return setError(message(r.code));
    onChanged();
    onClose();
  }

  return (
    <Modal title={`${host.ip} · ${host.mac}`} onClose={onClose} wide>
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
          <span className="flex items-center gap-2">
            <Dot online={host.online} />
            {host.online ? "Online" : "Offline"} for {ago(host.since)}
          </span>
          {host.hostname && <span className="font-mono text-xs text-fg-muted">{host.hostname}</span>}
          {host.node_id ? <InventoryBadge host={host} /> : <span className="text-xs text-fg-muted">not in the inventory</span>}
          <span className="text-xs text-fg-muted">first seen {when(host.first_seen)}</span>
        </div>

        {manage && (
          <form onSubmit={(e) => void saveLabel(e)} className="flex items-end gap-2">
            <div className="max-w-xs flex-1">
              <Field label="Label" hint="A name of your own, shown instead of the DNS name">
                <TextInput value={label} maxLength={128} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Office printer" />
              </Field>
            </div>
            <Button type="submit" disabled={busy || label.trim() === (host.label ?? "")}>
              Save
            </Button>
          </form>
        )}
        {error && <Alert>{error}</Alert>}

        <section>
          <div className="mb-2 flex items-center justify-between gap-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-fg-muted">Availability</h3>
            <Select value={hours} onChange={(e) => setHours(e.target.value)} className="!w-auto" aria-label="Period">
              {RANGES.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </Select>
          </div>
          {history.loading && !h && <Loading />}
          {history.error && <Alert>{history.error}</Alert>}
          {h && (
            <>
              <div className="relative h-7 w-full overflow-hidden rounded border border-line bg-canvas" role="img" aria-label="Availability over the period">
                {strip.map((b, i) => (
                  <div
                    key={i}
                    className={`absolute inset-y-0 ${b.online ? "bg-ok" : "bg-offline"}`}
                    style={{ left: `${b.left}%`, width: `${b.width}%` }}
                    title={`${b.online ? "Online" : "Offline"}: ${when(b.from)} → ${when(b.to)}`}
                  />
                ))}
              </div>
              <div className="mt-1 flex justify-between font-mono text-[10px] text-fg-muted">
                <span>{when(h.start)}</span>
                <span>{h.uptime_pct === null ? "no data in this period" : `up ${h.uptime_pct}% of the time it was known`}</span>
                <span>{when(h.end)}</span>
              </div>
            </>
          )}
        </section>

        {h && h.siblings.length > 0 && (
          <section>
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wider text-fg-muted">Same address or same MAC</h3>
            <ul className="divide-y divide-line text-sm">
              {h.siblings.map((s) => (
                <li key={s.id} className="flex items-center gap-3 py-1">
                  <Dot online={s.online} />
                  <span className={`font-mono text-xs ${s.ip === host.ip ? "" : "text-warn"}`}>{s.ip}</span>
                  <span className={`font-mono text-xs ${s.mac === host.mac ? "" : "text-warn"}`}>{s.mac}</span>
                  <span className="ml-auto text-[11px] text-fg-muted">last seen {when(s.last_seen)}</span>
                </li>
              ))}
            </ul>
          </section>
        )}

        {h && (
          <section>
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wider text-fg-muted">History</h3>
            {h.events.length === 0 ? (
              <p className="text-sm text-fg-muted">No change in this period.</p>
            ) : (
              <ul className="max-h-64 divide-y divide-line overflow-y-auto text-sm">
                {h.events.map((e) => (
                  <li key={e.id} className="flex items-center gap-3 py-1">
                    <span className="w-40 shrink-0 font-mono text-[11px] text-fg-muted">{when(e.at)}</span>
                    <KindBadge kind={e.kind} />
                    {e.prev && (
                      <span className="font-mono text-[11px] text-fg-muted">
                        {prevLabel(e.kind)} {e.prev}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        {manage && (
          <div className="flex justify-end">
            <Button variant="danger" onClick={() => void forget()}>
              <Trash2 className="h-3.5 w-3.5" aria-hidden /> Forget this host
            </Button>
          </div>
        )}
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- hosts tab
function Hosts({ subnets, manage }: { subnets: LanSubnet[]; manage: boolean }) {
  const sweeping = subnets.some((s) => s.sweeping || s.requested);
  const neverRun = subnets.some((s) => s.enabled && !s.last_run_at);
  const [status, setStatus] = useState<"all" | "online" | "offline">("all");
  const [subnetId, setSubnetId] = useState("");
  const [query, setQuery] = useState("");
  const [inventory, setInventory] = useState<"all" | "in" | "out">("all");
  const [open, setOpen] = useState<LanHost | null>(null);
  const hosts = useResource<LanHost[]>(`/lan/hosts?status=${status}${subnetId ? `&subnet_id=${subnetId}` : ""}`);
  usePolling(hosts.reload, sweeping || neverRun ? 3_000 : POLL_MS);
  const shown = useMemo(
    () => (hosts.data ?? []).filter((h) => matches(h, query) && (inventory === "all" || (inventory === "in") === !!h.node_id)),
    [hosts.data, query, inventory],
  );
  const up = (hosts.data ?? []).filter((h) => h.online).length;

  return (
    <div className="space-y-4">
      <Card>
        <Activity subnetId={subnetId} />
      </Card>
      <Card
        title={`Hosts · ${up} up of ${hosts.data?.length ?? 0} · ${(hosts.data ?? []).filter((h) => h.node_id).length} in the inventory`}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <TextInput value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search address, MAC, name…" aria-label="Search" className="!w-56" />
            <Select value={status} onChange={(e) => setStatus(e.target.value as typeof status)} className="!w-auto" aria-label="State">
              <option value="all">All</option>
              <option value="online">Online</option>
              <option value="offline">Offline</option>
            </Select>
            <Select value={inventory} onChange={(e) => setInventory(e.target.value as typeof inventory)} className="!w-auto" aria-label="Inventory">
              <option value="all">In inventory or not</option>
              <option value="in">In the inventory</option>
              <option value="out">Not in the inventory</option>
            </Select>
            <Select value={subnetId} onChange={(e) => setSubnetId(e.target.value)} className="!w-auto" aria-label="Network">
              <option value="">All networks</option>
              {subnets.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </Select>
          </div>
        }
      >
        {hosts.error && <Alert>{hosts.error}</Alert>}
        {hosts.loading && !hosts.data && <Loading />}
        {hosts.data && shown.length === 0 && (
          <p className="text-sm text-fg-muted">
            {hosts.data.length === 0
              ? subnets.length === 0
                ? "No host seen yet. Add a network in the Networks tab: the first sweep lists what is up."
                : sweeping || neverRun
                  ? "The first sweep is running: the hosts that are up appear here in a few seconds."
                  : subnets.some((s) => s.last_error)
                    ? "The last sweep failed: see the Networks tab for the reason."
                    : "No host is up on the watched networks."
              : "No host matches."}
          </p>
        )}
        {shown.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-[11px] uppercase text-fg-muted">
                <tr>
                  <th className="py-1 pr-3 font-medium">State</th>
                  <th className="py-1 pr-3 font-medium">Address</th>
                  <th className="py-1 pr-3 font-medium">MAC</th>
                  <th className="py-1 pr-3 font-medium">Name</th>
                  <th className="py-1 pr-3 font-medium">Network</th>
                  <th className="py-1 pr-3 font-medium">For</th>
                  <th className="py-1 font-medium">Last seen</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {shown.map((h) => (
                  <tr key={h.id} onClick={() => setOpen(h)} className={`cursor-pointer transition-colors hover:bg-elevated/50 ${h.online ? "" : "text-fg-muted"}`}>
                    <td className="py-1.5 pr-3">
                      <span className="flex items-center gap-2">
                        <Dot online={h.online} />
                        <span className="text-xs">{h.online ? "up" : "down"}</span>
                      </span>
                    </td>
                    <td className="py-1.5 pr-3 font-mono text-xs">
                      <button type="button" onClick={() => setOpen(h)} className="hover:underline focus-visible:underline">
                        {h.ip}
                      </button>
                    </td>
                    <td className="py-1.5 pr-3 font-mono text-xs">
                      {h.mac}
                      {h.macs_on_ip > 1 && (
                        <span className="ml-2 rounded bg-warn/15 px-1 py-0.5 text-[10px] font-medium text-warn" title="This address has answered from several MACs">
                          {h.macs_on_ip} MACs
                        </span>
                      )}
                      {h.ips_of_mac > 1 && (
                        <span className="ml-2 rounded bg-warn/15 px-1 py-0.5 text-[10px] font-medium text-warn" title="This MAC has been seen on several addresses">
                          {h.ips_of_mac} addresses
                        </span>
                      )}
                    </td>
                    <td className="py-1.5 pr-3">
                      {displayName(h) ?? <span className="text-fg-muted">—</span>}
                      {h.node_id && (
                        <span className="ml-2">
                          <InventoryBadge host={h} />
                        </span>
                      )}
                    </td>
                    <td className="py-1.5 pr-3 text-xs">{h.subnet ?? "—"}</td>
                    <td className="whitespace-nowrap py-1.5 pr-3 text-xs">{ago(h.since)}</td>
                    <td className="whitespace-nowrap py-1.5 font-mono text-[11px] text-fg-muted">{when(h.last_seen)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {open && <HostDetail host={open} manage={manage} onClose={() => setOpen(null)} onChanged={hosts.reload} />}
    </div>
  );
}

// ---------------------------------------------------------------- events tab
function Events() {
  const [kind, setKind] = useState("");
  const [hours, setHours] = useState("168");
  const events = useResource<LanEvent[]>(`/lan/events?hours=${hours}&limit=500${kind ? `&kind=${kind}` : ""}`);
  usePolling(events.reload);
  return (
    <Card
      title="Changes"
      actions={
        <div className="flex items-center gap-2">
          <Select value={kind} onChange={(e) => setKind(e.target.value)} className="!w-auto" aria-label="Kind of change">
            <option value="">All changes</option>
            {(Object.keys(KIND_LABEL) as LanEventKind[]).map((k) => (
              <option key={k} value={k}>
                {KIND_LABEL[k]}
              </option>
            ))}
          </Select>
          <Select value={hours} onChange={(e) => setHours(e.target.value)} className="!w-auto" aria-label="Period">
            {RANGES.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </Select>
        </div>
      }
    >
      {events.error && <Alert>{events.error}</Alert>}
      {events.loading && !events.data && <Loading />}
      {events.data?.length === 0 && <p className="text-sm text-fg-muted">No change in this period.</p>}
      {events.data && events.data.length > 0 && (
        <table className="w-full text-left text-sm">
          <thead className="text-[11px] uppercase text-fg-muted">
            <tr>
              <th className="py-1 pr-3 font-medium">Time</th>
              <th className="py-1 pr-3 font-medium">Change</th>
              <th className="py-1 pr-3 font-medium">Address</th>
              <th className="py-1 pr-3 font-medium">MAC</th>
              <th className="py-1 font-medium">Detail</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {events.data.map((e) => (
              <tr key={e.id}>
                <td className="whitespace-nowrap py-1.5 pr-3 font-mono text-[11px] text-fg-muted">{when(e.at)}</td>
                <td className="py-1.5 pr-3">
                  <KindBadge kind={e.kind} />
                </td>
                <td className="py-1.5 pr-3 font-mono text-xs">{e.ip}</td>
                <td className="py-1.5 pr-3 font-mono text-xs">{e.mac}</td>
                <td className="py-1.5 text-xs text-fg-muted">
                  {displayName(e)}
                  {e.prev && (
                    <span className="ml-2 font-mono">
                      {prevLabel(e.kind)} {e.prev}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- networks tab
function SubnetDialog({ subnet, onClose, onSaved }: { subnet: LanSubnet | null; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(subnet?.name ?? "");
  const [cidr, setCidr] = useState(subnet?.cidr ?? "");
  const [interval, setInterval_] = useState(String(subnet?.interval_minutes ?? 5));
  const [enabled, setEnabled] = useState(subnet?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const body = { name: name.trim(), interval_minutes: Number(interval), enabled };
    const r = subnet
      ? await api(`/lan/subnets/${subnet.id}`, { method: "PATCH", body })
      : await api("/lan/subnets", { method: "POST", body: { ...body, cidr: cidr.trim() } });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    onSaved();
    onClose();
  }

  return (
    <Modal title={subnet ? `Edit ${subnet.name}` : "Watch a network"} onClose={onClose}>
      <form onSubmit={(e) => void save(e)} className="space-y-3">
        <Field label="Name">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Office LAN" required autoFocus />
        </Field>
        <Field label="Network" hint="CIDR, at most a /22 (1024 addresses). The server must be attached to it.">
          <TextInput value={cidr} onChange={(e) => setCidr(e.target.value)} placeholder="192.168.0.0/24" disabled={!!subnet} required />
        </Field>
        <Field label="Sweep every">
          <Select value={interval} onChange={(e) => setInterval_(e.target.value)}>
            {INTERVALS.map((m) => (
              <option key={m} value={m}>
                {intervalLabel(m)}
              </option>
            ))}
          </Select>
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Sweep in the background
        </label>
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy || !name.trim() || !cidr.trim()}>
            {busy ? "Saving…" : "Save"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** A scan of a network started by hand: the result is saved (hosts and history) like a sweep. */
function ScanDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [cidr, setCidr] = useState("");
  const [name, setName] = useState("");
  const [keep, setKeep] = useState(false);
  const [interval, setInterval_] = useState("5");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  async function run(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const created = await api<{ id: string }>("/lan/subnets", {
      method: "POST",
      body: { name: name.trim() || cidr.trim(), cidr: cidr.trim(), interval_minutes: Number(interval), enabled: keep },
    });
    if (!created.ok) {
      setBusy(false);
      return setError(message(created.code));
    }
    const queued = await api(`/lan/subnets/${created.data.id}/scan`, { method: "POST" });
    let failed: string | null = queued.ok ? null : message(queued.code);
    let found: LanSubnet | undefined;
    // the scanner may be another container: wait for it to take the request and finish (about 10 s)
    for (let i = 0; i < 60 && queued.ok && !found; i++) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      const list = await api<LanSubnet[]>("/lan/subnets");
      const s = list.ok ? list.data.find((x) => x.id === created.data.id) : undefined;
      if (s && !s.requested && !s.sweeping && s.last_run_at) found = s;
    }
    if (queued.ok && !found) failed = "The scanner did not answer in two minutes. Is it running? (compose profile lan-watch)";
    if (found?.last_error) failed = message(found.last_error);
    if (failed && !keep) await api(`/lan/subnets/${created.data.id}`, { method: "DELETE" }); // nothing worth keeping
    setBusy(false);
    onDone();
    if (failed) return setError(failed);
    if (found) setResult(`${found.last_found} hosts up, ${found.last_new} new. The result is saved: see the Hosts and Changes tabs.`);
  }

  return (
    <Modal title="Scan a network now" onClose={onClose}>
      {result ? (
        <div className="space-y-3">
          <Alert kind="ok">{result}</Alert>
          <div className="flex justify-end">
            <Button onClick={onClose}>Close</Button>
          </div>
        </div>
      ) : (
        <form onSubmit={(e) => void run(e)} className="space-y-3">
          <Field label="Network" hint="CIDR, at most a /22. The server must be attached to it. A scan takes about ten seconds.">
            <TextInput value={cidr} onChange={(e) => setCidr(e.target.value)} placeholder="192.168.0.0/24" required autoFocus />
          </Field>
          <Field label="Name" hint="Optional: the network itself by default">
            <TextInput value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} /> Keep watching it
          </label>
          {keep && (
            <Field label="Sweep every">
              <Select value={interval} onChange={(e) => setInterval_(e.target.value)}>
                {INTERVALS.map((m) => (
                  <option key={m} value={m}>
                    {intervalLabel(m)}
                  </option>
                ))}
              </Select>
            </Field>
          )}
          <p className="text-[11px] text-fg-muted">
            Without it the network is saved as paused: the hosts and their history stay, and you can sweep it again from the list.
          </p>
          {error && <Alert>{error}</Alert>}
          <div className="flex justify-end gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={busy || !cidr.trim()}>
              {busy ? (
                <>
                  <Spinner className="!h-3 !w-3" /> Scanning…
                </>
              ) : (
                "Scan and save"
              )}
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}

function Retention() {
  const settings = useResource<{ retention_days: number }>("/lan/settings");
  const [days, setDays] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const value = days || String(settings.data?.retention_days ?? "");

  async function save(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSaved(false);
    const r = await api("/lan/settings", { method: "PUT", body: { retention_days: Number(value) } });
    if (!r.ok) return setError(message(r.code));
    setSaved(true);
    settings.reload();
  }

  return (
    <Card title="History">
      <form onSubmit={(e) => void save(e)} className="flex flex-wrap items-end gap-3">
        <div className="w-48">
          <Field label="Keep the history for (days)" hint="Older changes, and hosts not seen since, are deleted.">
            <TextInput type="number" min={1} max={730} value={value} onChange={(e) => setDays(e.target.value)} />
          </Field>
        </div>
        <Button type="submit" disabled={!/^\d+$/.test(value) || Number(value) < 1 || Number(value) > 730}>
          Save
        </Button>
      </form>
      {error && <Alert>{error}</Alert>}
      {saved && <Alert kind="ok">Saved.</Alert>}
    </Card>
  );
}

function Networks({ subnets, manage }: { subnets: ReturnType<typeof useResource<LanSubnet[]>>; manage: boolean }) {
  const [editing, setEditing] = useState<LanSubnet | "new" | null>(null);
  const [scanning, setScanning] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  async function scan(s: LanSubnet) {
    setBusy(s.id);
    setNotice(null);
    const r = await api(`/lan/subnets/${s.id}/scan`, { method: "POST" });
    setBusy(null);
    if (!r.ok) setNotice({ kind: "error", text: message(r.code) });
    else setNotice({ kind: "ok", text: `${s.name}: sweep requested. The scanner starts it within a few seconds; the result appears in this list.` });
    subnets.reload();
  }

  async function remove(s: LanSubnet) {
    if (!window.confirm(`Stop watching ${s.name}? The hosts and their history are kept until the retention deletes them.`)) return;
    const r = await api(`/lan/subnets/${s.id}`, { method: "DELETE" });
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    subnets.reload();
  }

  return (
    <div className="space-y-4">
      <Card
        title="Networks"
        actions={
          manage && (
            <div className="flex gap-2">
              <Button onClick={() => setScanning(true)}>
                <Radar className="h-3.5 w-3.5" aria-hidden /> Scan a network now
              </Button>
              <Button variant="primary" onClick={() => setEditing("new")}>
                <Plus className="h-3.5 w-3.5" aria-hidden /> Watch a network
              </Button>
            </div>
          )
        }
      >
        {notice && <Alert kind={notice.kind === "ok" ? "ok" : "error"}>{notice.text}</Alert>}
        {subnets.error && <Alert>{subnets.error}</Alert>}
        {subnets.data?.length === 0 && (
          <p className="text-sm text-fg-muted">
            No network yet. The server sweeps a network it is attached to with ARP and ping, so a scanner on the host network does it: turn on the{" "}
            <span className="font-mono">lan-watch</span> profile of the compose file.
          </p>
        )}
        {subnets.data && subnets.data.length > 0 && (
          <table className="w-full text-left text-sm">
            <thead className="text-[11px] uppercase text-fg-muted">
              <tr>
                <th className="py-1 pr-3 font-medium">Name</th>
                <th className="py-1 pr-3 font-medium">Network</th>
                <th className="py-1 pr-3 font-medium">Every</th>
                <th className="py-1 pr-3 font-medium">Up</th>
                <th className="py-1 pr-3 font-medium">Last sweep</th>
                <th className="py-1" />
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {subnets.data.map((s) => (
                <tr key={s.id} className={s.enabled ? "" : "text-fg-muted"}>
                  <td className="py-1.5 pr-3">
                    {s.name}
                    {!s.enabled && <span className="ml-2 text-[11px]">paused</span>}
                  </td>
                  <td className="py-1.5 pr-3 font-mono text-xs">{s.cidr}</td>
                  <td className="py-1.5 pr-3 text-xs">{intervalLabel(s.interval_minutes)}</td>
                  <td className="py-1.5 pr-3 text-xs">
                    {s.hosts_online} / {s.hosts_total}
                  </td>
                  <td className="py-1.5 pr-3 text-xs">
                    {s.sweeping || s.requested ? (
                      <div className="flex items-center gap-1.5 text-primary">
                        <Spinner className="!h-3 !w-3" /> {s.requested ? "Waiting for the scanner…" : "Sweeping now…"}
                      </div>
                    ) : (
                      <div className="font-mono text-[11px] text-fg-muted">{when(s.last_run_at)}</div>
                    )}
                    {s.last_error && <div className="max-w-md text-warn">{message(s.last_error)}</div>}
                    {!s.last_error && !s.sweeping && !s.requested && s.last_found !== null && (
                      <div className="text-fg-muted">
                        {s.last_found} up{s.last_new ? `, ${s.last_new} new` : ""}
                      </div>
                    )}
                  </td>
                  <td className="py-1.5 text-right">
                    {manage && (
                      <span className="inline-flex gap-1">
                        <Button aria-label={`Sweep ${s.name} now`} title="Sweep now" disabled={busy === s.id || s.sweeping || s.requested} onClick={() => void scan(s)}>
                          <Play className="h-3.5 w-3.5" aria-hidden /> {s.sweeping || s.requested ? "Sweeping…" : "Sweep now"}
                        </Button>
                        <Button aria-label={`Edit ${s.name}`} onClick={() => setEditing(s)}>
                          <Pencil className="h-3.5 w-3.5" aria-hidden />
                        </Button>
                        <Button variant="danger" aria-label={`Remove ${s.name}`} onClick={() => void remove(s)}>
                          <Trash2 className="h-3.5 w-3.5" aria-hidden />
                        </Button>
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      {manage && <Retention />}
      {scanning && <ScanDialog onClose={() => setScanning(false)} onDone={subnets.reload} />}
      {editing && <SubnetDialog subnet={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={subnets.reload} />}
    </div>
  );
}

// ---------------------------------------------------------------- page body
export function LanWatch() {
  const { can } = useAuth();
  const manage = can("inventory.manage");
  const [tab, setTab] = useState<"hosts" | "events" | "networks">("hosts");
  const subnets = useResource<LanSubnet[]>("/lan/subnets");
  const scanner = useResource<LanScanner>("/lan/scanner");
  usePolling(scanner.reload, 15_000);
  const list = subnets.data ?? [];
  usePolling(subnets.reload, list.some((s) => s.sweeping) ? 3_000 : 10_000);
  const up = list.reduce((n, s) => n + s.hosts_online, 0);
  const total = list.reduce((n, s) => n + s.hosts_total, 0);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 rounded-lg border border-line bg-surface px-4 py-2.5 text-xs">
        <span className="font-semibold uppercase tracking-wider text-fg-muted">Networks</span>
        <span className="font-mono">{list.filter((s) => s.enabled).length} watched</span>
        <span className="flex items-center gap-2 text-ok">
          <Dot online /> {up} up
        </span>
        <span className="flex items-center gap-2 text-fg-muted">
          <Dot online={false} /> {total - up} down
        </span>
        {list.some((s) => s.sweeping) && (
          <span className="flex items-center gap-2 text-primary">
            <Spinner className="!h-3 !w-3" /> sweep in progress
          </span>
        )}
        {list.some((s) => s.last_error) && <span className="text-warn">a network could not be swept (Networks tab)</span>}
      </div>
      {scanner.data && !scanner.data.running && (
        <Alert kind="info">
          {scanner.data.mode === "external" ? (
            <>
              No scanner is running, so networks are not swept. Turn on the <span className="font-mono">lan-watch</span> profile of the compose
              file (<span className="font-mono">COMPOSE_PROFILES=lan-watch</span> in <span className="font-mono">deploy/.env</span>) and start the stack again.
            </>
          ) : (
            "The scanner has not reported yet. If this stays, check the server log."
          )}
        </Alert>
      )}
      <div role="tablist" className="flex gap-1 border-b border-line">
        {(["hosts", "events", "networks"] as const).map((t) => (
          <button
            key={t}
            role="tab"
            type="button"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-3 py-1.5 text-sm transition-colors ${tab === t ? "border-primary text-fg" : "border-transparent text-fg-muted hover:text-fg"}`}
          >
            {t === "hosts" ? "Hosts" : t === "events" ? "Changes" : "Networks"}
          </button>
        ))}
      </div>
      {tab === "hosts" && <Hosts subnets={list} manage={manage} />}
      {tab === "events" && <Events />}
      {tab === "networks" && <Networks subnets={subnets} manage={manage} />}
    </div>
  );
}
