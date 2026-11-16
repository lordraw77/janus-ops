"use client";

import { useState } from "react";

import { Alert, Button, Field, Modal, Select } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";

type TemplateItem = {
  name: string;
  oid: string;
  indexed: boolean;
  comparison: "gt" | "lt" | null;
  warn: number | null;
  crit: number | null;
};

/** A check template: a named set of SNMP checks for one kind of device. */
export type SnmpTemplate = {
  id: string;
  name: string;
  device_type: string;
  description: string;
  items: TemplateItem[];
  source: string;
};

type SnmpNode = { id: string; name: string };

type ApplyResult = {
  created: string[];
  skipped: { name: string; oid: string; reason: string }[];
};

type Discovery = {
  candidates: { oid: string; indexed: boolean; instances: number[] }[];
};

/** A short line describing one item's thresholds, for the template list. */
function thresholds(item: TemplateItem): string {
  if (!item.comparison) return "";
  const op = item.comparison === "gt" ? ">" : "<";
  const parts = [];
  if (item.warn !== null) parts.push(`warn ${op} ${item.warn}`);
  if (item.crit !== null) parts.push(`crit ${op} ${item.crit}`);
  return parts.join(", ");
}

/** Apply a template to a node: the table items need the instances the node really has. */
export function SnmpTemplatesDialog({ onClose, onApplied }: { onClose: () => void; onApplied: () => void }) {
  const templates = useResource<SnmpTemplate[]>("/snmp-templates");
  const nodes = useResource<SnmpNode[]>("/nodes?capability=snmp");
  const [templateId, setTemplateId] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [instances, setInstances] = useState<Record<string, number[]> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ApplyResult | null>(null);

  const template = (templates.data ?? []).find((t) => t.id === templateId) ?? null;

  async function readInstances() {
    setBusy(true);
    setError(null);
    setInstances(null);
    const r = await api<Discovery>("/snmp-oids/discover", { method: "POST", body: { node_id: nodeId } });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    const found: Record<string, number[]> = {};
    for (const c of r.data.candidates) if (c.indexed) found[c.oid] = c.instances;
    setInstances(found);
  }

  async function apply() {
    if (!template) return;
    setBusy(true);
    setError(null);
    const r = await api<ApplyResult>(`/snmp-templates/${template.id}/apply`, {
      method: "POST",
      body: { node_id: nodeId, instances: instances ?? {} },
    });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    setResult(r.data);
    onApplied();
  }

  return (
    <Modal title="Check templates" onClose={onClose} wide>
      <div className="space-y-4">
        {error && <Alert>{error}</Alert>}
        <Field label="Template">
          <Select value={templateId} onChange={(e) => { setTemplateId(e.target.value); setResult(null); }}>
            <option value="">Choose a template</option>
            {(templates.data ?? []).map((t) => (
              <option key={t.id} value={t.id}>
                {t.name} ({t.device_type})
              </option>
            ))}
          </Select>
        </Field>
        {template && (
          <div className="space-y-2 rounded border border-line p-3">
            <p className="text-xs text-fg-muted">{template.description}</p>
            <table className="w-full text-left text-xs">
              <thead className="text-fg-muted">
                <tr>
                  <th className="py-1">Check</th>
                  <th className="py-1">OID</th>
                  <th className="py-1">Per instance</th>
                  <th className="py-1">Thresholds</th>
                </tr>
              </thead>
              <tbody>
                {template.items.map((item) => (
                  <tr key={item.oid} className="border-t border-line">
                    <td className="py-1">{item.name}</td>
                    <td className="py-1 font-mono text-[11px]">{item.oid}</td>
                    <td className="py-1">{item.indexed ? "yes" : "no"}</td>
                    <td className="py-1">{thresholds(item)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex items-end gap-2">
          <Field label="Node">
            <Select
              value={nodeId}
              onChange={(e) => {
                setNodeId(e.target.value);
                setInstances(null);
                setResult(null);
              }}
            >
              <option value="">Choose an SNMP node</option>
              {(nodes.data ?? []).map((n) => (
                <option key={n.id} value={n.id}>
                  {n.name}
                </option>
              ))}
            </Select>
          </Field>
          <Button type="button" onClick={() => void readInstances()} disabled={busy || !nodeId || !template}>
            Read instances from the node
          </Button>
        </div>
        {instances && (
          <div className="text-xs text-fg-muted">
            {Object.keys(instances).length === 0
              ? "No table instances found: only the scalar checks will be created."
              : Object.entries(instances).map(([oid, list]) => (
                  <div key={oid}>
                    <span className="font-mono">{oid}</span>: instances {list.join(", ")}
                  </div>
                ))}
          </div>
        )}
        <div className="flex justify-end">
          <Button
            type="button"
            variant="primary"
            onClick={() => void apply()}
            disabled={busy || !nodeId || !template}
          >
            {busy ? "Working…" : "Apply to node"}
          </Button>
        </div>
        {result && (
          <div className="space-y-2 rounded border border-line p-3 text-xs">
            <div className="text-ok">{result.created.length} checks created.</div>
            {result.created.length > 0 && <div>{result.created.join(", ")}</div>}
            {result.skipped.length > 0 && (
              <div className="space-y-1 text-fg-muted">
                <div>{result.skipped.length} skipped:</div>
                {result.skipped.map((s) => (
                  <div key={`${s.oid}-${s.name}`}>
                    {s.name}: {s.reason}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}
