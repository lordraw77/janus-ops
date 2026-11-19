"use client";

import { type FormEvent, useState } from "react";

import { Alert, Button, Field, Modal, Select, TextInput } from "@/components/ui";
import { TemplateApplyDialog } from "@/components/TemplateApply";
import { api, message, useResource } from "@/lib/api";

/** One catalog entry, as the API returns it. */
export type OidEntry = {
  id: string;
  name: string;
  oid: string;
  unit: string;
  description: string;
  indexed: boolean;
  verified: boolean;
  device_type: DeviceType;
  vendor: string;
  source: string;
};

export const DEVICE_TYPES = ["nas", "ups", "switch", "router", "server", "generic"] as const;
export type DeviceType = (typeof DEVICE_TYPES)[number];

/** A proposal from a discovery walk. */
type Candidate = {
  oid: string;
  indexed: boolean;
  instances: number[];
  sample: string;
  number: number | null;
  example: string;
  in_catalog: boolean;
};

type Discovery = {
  walks: { root: string; answers: number; truncated: boolean; error: string | null }[];
  candidates: Candidate[];
};

type SnmpNode = { id: string; name: string };

/** The catalog, loaded once; `reload` after a change. */
export function useOidCatalog() {
  return useResource<OidEntry[]>("/snmp-oids");
}

const CHECKBOX = "h-4 w-4 accent-primary";
const WALK_ERRORS: Record<string, string> = {
  timeout: "no answer from the device (check the address, the port and the SNMP service)",
  authentication_failed: "SNMPv3 authentication failed",
  walk_timeout: "the walk took too long; the answers so far are listed",
  request_failed: "the request failed",
  pdu_error: "the device answered with an SNMP error",
};

/** Walk a node's SNMP agent and pick what to add to the catalog. */
function Discover({ onAdded }: { onAdded: () => void }) {
  const nodes = useResource<SnmpNode[]>("/nodes?capability=snmp");
  const [nodeId, setNodeId] = useState("");
  const [result, setResult] = useState<Discovery | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [deviceType, setDeviceType] = useState<DeviceType>("nas");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<string | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    setReport(null);
    setPicked(new Set());
    const r = await api<Discovery>("/snmp-oids/discover", { method: "POST", body: { node_id: nodeId } });
    setBusy(false);
    if (!r.ok) {
      setResult(null);
      return setError(message(r.code));
    }
    setResult(r.data);
    // what is new is selected by default; what is already in the catalog is not
    setPicked(new Set(r.data.candidates.filter((c) => !c.in_catalog).map((c) => c.oid)));
  }

  async function addPicked() {
    if (!result) return;
    setBusy(true);
    setError(null);
    let added = 0;
    const failed: string[] = [];
    for (const cand of result.candidates.filter((c) => picked.has(c.oid))) {
      const name = `${cand.sample || "SNMP value"}`.slice(0, 60).trim() + ` [${cand.oid}]`;
      const r = await api("/snmp-oids", {
        method: "POST",
        body: { name, oid: cand.oid, indexed: cand.indexed, device_type: deviceType, source: "discovery" },
      });
      if (r.ok) added += 1;
      else failed.push(`${cand.oid}: ${message(r.code)}`);
    }
    setBusy(false);
    setReport(`${added} added${failed.length ? `, ${failed.length} not added` : ""}.`);
    if (failed.length) setError(failed.join("\n"));
    onAdded();
    void run();
  }

  const togglePick = (oid: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(oid)) next.delete(oid);
      else next.add(oid);
      return next;
    });

  return (
    <div className="space-y-3 rounded border border-line p-3">
      <div className="text-sm font-medium">Discover on a node</div>
      <p className="text-[11px] text-fg-muted">
        Reads the node&apos;s SNMP agent (GETNEXT only, nothing is changed on the device). Nothing reaches the
        catalog until you select it.
      </p>
      <div className="flex gap-2">
        <Select className="flex-1" value={nodeId} onChange={(e) => setNodeId(e.target.value)}>
          <option value="">Choose an SNMP node</option>
          {(nodes.data ?? []).map((n) => (
            <option key={n.id} value={n.id}>
              {n.name}
            </option>
          ))}
        </Select>
        <Button type="button" onClick={() => void run()} disabled={busy || !nodeId}>
          {busy ? "Walking…" : "Discover"}
        </Button>
      </div>
      {error && <Alert>{error}</Alert>}
      {report && <div className="text-xs text-ok">{report}</div>}
      {result && (
        <>
          <div className="space-y-1 text-[11px] text-fg-muted">
            {result.walks.map((w) => (
              <div key={w.root}>
                <span className="font-mono">{w.root}</span>: {w.answers} answers
                {w.truncated && " (capped, more exist)"}
                {w.error && <span className="text-crit"> · {WALK_ERRORS[w.error] ?? w.error}</span>}
              </div>
            ))}
          </div>
          <div className="max-h-[30vh] overflow-y-auto rounded border border-line">
            <table className="w-full text-left text-xs">
              <thead className="sticky top-0 bg-surface text-fg-muted">
                <tr>
                  <th className="px-2 py-1" />
                  <th className="px-2 py-1">OID</th>
                  <th className="px-2 py-1">Kind</th>
                  <th className="px-2 py-1">Value (first answer)</th>
                </tr>
              </thead>
              <tbody>
                {result.candidates.map((c) => (
                  <tr key={c.oid} className="border-t border-line">
                    <td className="px-2 py-1">
                      <input
                        type="checkbox"
                        className={CHECKBOX}
                        disabled={c.in_catalog}
                        checked={picked.has(c.oid)}
                        onChange={() => togglePick(c.oid)}
                      />
                    </td>
                    <td className="px-2 py-1 font-mono text-[11px]">{c.oid}</td>
                    <td className="px-2 py-1">
                      {c.indexed ? `table, ${c.instances.length} instance${c.instances.length === 1 ? "" : "s"}` : "scalar"}
                      {c.in_catalog && <span className="ml-1 text-ok">in catalog</span>}
                    </td>
                    <td className="max-w-[16rem] truncate px-2 py-1">{c.sample}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex items-end justify-between gap-3">
            <Field label="Device type for the new entries">
              <Select value={deviceType} onChange={(e) => setDeviceType(e.target.value as DeviceType)}>
                {DEVICE_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </Select>
            </Field>
            <Button type="button" variant="primary" onClick={() => void addPicked()} disabled={busy || picked.size === 0}>
              Add {picked.size} to catalog
            </Button>
          </div>
        </>
      )}
    </div>
  );
}

/** The OID catalog: browse by device type, add, verify on a device, delete, discover. */
export function SnmpOidCatalog({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const [filter, setFilter] = useState<DeviceType | "">("");
  const catalog = useResource<OidEntry[]>(filter ? `/snmp-oids?device_type=${filter}` : "/snmp-oids");
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [oid, setOid] = useState("");
  const [unit, setUnit] = useState("");
  const [description, setDescription] = useState("");
  const [indexed, setIndexed] = useState(false);
  const [deviceType, setDeviceType] = useState<DeviceType>("nas");
  const [busy, setBusy] = useState(false);
  const [templatesOpen, setTemplatesOpen] = useState(false);

  async function add(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const r = await api("/snmp-oids", {
      method: "POST",
      body: {
        name: name.trim(),
        oid: oid.trim(),
        unit: unit.trim(),
        description: description.trim(),
        indexed,
        device_type: deviceType,
      },
    });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    setName("");
    setOid("");
    setUnit("");
    setDescription("");
    setIndexed(false);
    catalog.reload();
    onChanged();
  }

  async function toggleVerified(entry: OidEntry) {
    setError(null);
    const r = await api(`/snmp-oids/${entry.id}`, { method: "PATCH", body: { verified: !entry.verified } });
    if (!r.ok) return setError(message(r.code));
    catalog.reload();
    onChanged();
  }

  async function remove(entry: OidEntry) {
    setError(null);
    const r = await api(`/snmp-oids/${entry.id}`, { method: "DELETE" });
    if (!r.ok) return setError(message(r.code));
    catalog.reload();
    onChanged();
  }

  return (
    <Modal title="SNMP OID catalog" onClose={onClose} wide>
      <div className="space-y-4">
        {error && <Alert>{error}</Alert>}
        {catalog.error && <Alert>{catalog.error}</Alert>}
        <div className="flex items-center justify-between gap-3">
          <Field label="Device type">
            <Select value={filter} onChange={(e) => setFilter(e.target.value as DeviceType | "")}>
              <option value="">All types</option>
              {DEVICE_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </Select>
          </Field>
          <div className="flex items-center gap-3">
            <span className="text-[11px] text-fg-muted">{(catalog.data ?? []).length} entries</span>
            <Button type="button" onClick={() => setTemplatesOpen(true)}>
              Check templates
            </Button>
          </div>
        </div>
        <div className="max-h-[32vh] overflow-y-auto rounded border border-line">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-surface text-fg-muted">
              <tr>
                <th className="px-2 py-1.5">Name</th>
                <th className="px-2 py-1.5">OID</th>
                <th className="px-2 py-1.5">Type</th>
                <th className="px-2 py-1.5">Unit</th>
                <th className="px-2 py-1.5">Verified</th>
                <th className="px-2 py-1.5" />
              </tr>
            </thead>
            <tbody>
              {(catalog.data ?? []).map((entry) => (
                <tr key={entry.id} className="border-t border-line align-top">
                  <td className="px-2 py-1.5">
                    <div>{entry.name}</div>
                    {entry.description && <div className="text-[11px] text-fg-muted">{entry.description}</div>}
                    {entry.indexed && <div className="text-[11px] text-fg-muted">table column: needs an instance number</div>}
                  </td>
                  <td className="px-2 py-1.5 font-mono text-[11px]">{entry.oid}</td>
                  <td className="px-2 py-1.5">{entry.device_type}</td>
                  <td className="px-2 py-1.5">{entry.unit}</td>
                  <td className="px-2 py-1.5">
                    <button
                      type="button"
                      onClick={() => void toggleVerified(entry)}
                      className={entry.verified ? "text-ok" : "text-fg-muted underline"}
                      title="Mark as read on a real device"
                    >
                      {entry.verified ? "yes" : "not yet"}
                    </button>
                  </td>
                  <td className="px-2 py-1.5 text-right">
                    <button type="button" onClick={() => void remove(entry)} className="text-crit hover:underline">
                      delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <Discover
          onAdded={() => {
            catalog.reload();
            onChanged();
          }}
        />
        <form onSubmit={(e) => void add(e)} className="grid grid-cols-2 gap-3 rounded border border-line p-3">
          <div className="col-span-2 text-sm font-medium">Add by hand</div>
          <Field label="Name">
            <TextInput required value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Synology disk temperature" />
          </Field>
          <Field label="OID" hint="Numeric, e.g. 1.3.6.1.4.1.6574.1.2.0">
            <TextInput required value={oid} onChange={(e) => setOid(e.target.value)} placeholder="1.3.6.1.4.1.6574.1.2.0" />
          </Field>
          <Field label="Device type">
            <Select value={deviceType} onChange={(e) => setDeviceType(e.target.value as DeviceType)}>
              {DEVICE_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Unit" hint="Optional, e.g. °C or %">
            <TextInput value={unit} onChange={(e) => setUnit(e.target.value)} />
          </Field>
          <Field label="Description">
            <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" className={CHECKBOX} checked={indexed} onChange={(e) => setIndexed(e.target.checked)} />
            Table column (needs an instance number)
          </label>
          <div className="col-span-2 flex justify-end">
            <Button type="submit" variant="primary" disabled={busy || !name.trim() || !oid.trim()}>
              Add to catalog
            </Button>
          </div>
        </form>
      </div>
      {templatesOpen && (
        <TemplateApplyDialog
          requires="snmp"
          onClose={() => setTemplatesOpen(false)}
          onApplied={() => {
            onChanged();
          }}
        />
      )}
    </Modal>
  );
}
