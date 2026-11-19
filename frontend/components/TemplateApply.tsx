"use client";

import { useState } from "react";

import { Alert, Button, Field, Modal, Select, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";

export type MacroValue = string | number | boolean;

export type Macro = {
  name: string;
  default: MacroValue;
  description?: string;
  auto?: Record<string, unknown>;
};

export type TemplateItem = {
  name: string;
  kind: string;
  params: Record<string, unknown>;
  indexed?: boolean;
  comparison?: "gt" | "lt" | null;
  warn?: number | string | null;
  crit?: number | string | null;
  interval_s?: number;
  check_cycles?: number;
};

export type Detect = { processes: string[]; ports: number[]; units: string[] };

/** A check template: a named bundle of checks with macros, for a service or a device. */
export type Template = {
  id: string;
  slug: string | null;
  name: string;
  category: string;
  description: string;
  requires: string | null;
  macros: Macro[];
  items: TemplateItem[];
  detect: Detect;
  source: "built-in" | "manual";
  version: number;
  node_count?: number;
  created_by: string;
  updated_at: string;
};

export type TemplateNode = {
  node_id: string;
  node_name: string;
  address: string;
  macros: Record<string, { value: MacroValue; origin: "manual" | "auto" }>;
  checks: number;
  applied_version: number;
  applied_by: string;
  applied_at: string;
  update_available: boolean;
};

type NodeRow = { id: string; name: string };

type ApplyResult = {
  created: string[];
  skipped: { name: string; reason: string }[];
  dry_run: boolean;
};

type Discovery = { candidates: { oid: string; indexed: boolean; instances: number[] }[] };

/** Where a macro value comes from, when it is not the default. */
function originLabel(origin: string | undefined): string {
  if (origin === "auto") return " · found by the agent";
  if (origin === "manual") return " · set by you";
  return "";
}

/** A short line describing one item's thresholds, for lists. */
export function thresholds(item: TemplateItem): string {
  if (!item.comparison) return "";
  const op = item.comparison === "gt" ? ">" : "<";
  const parts = [];
  if (item.warn !== null && item.warn !== undefined) parts.push(`warn ${op} ${item.warn}`);
  if (item.crit !== null && item.crit !== undefined) parts.push(`crit ${op} ${item.crit}`);
  return parts.join(", ");
}

/** Apply a template to a node: macro values, the table instances a node really has, a preview. */
export function TemplateApplyDialog({
  template,
  requires,
  nodeId: initialNode,
  onClose,
  onApplied,
}: {
  /** the template to apply; without it the dialog offers the ones that need `requires` */
  template?: Template;
  requires?: string;
  nodeId?: string;
  onClose: () => void;
  onApplied: () => void;
}) {
  const list = useResource<Template[]>(template ? null : `/templates${requires ? `?requires=${requires}` : ""}`);
  const [nodeId, setNodeId] = useState(initialNode ?? "");
  const [templateId, setTemplateId] = useState(template?.id ?? "");
  const current = template ?? (list.data ?? []).find((t) => t.id === templateId) ?? null;
  const capability = current?.requires ?? requires ?? null;
  const nodes = useResource<NodeRow[]>(capability ? `/nodes?capability=${capability}` : "/nodes");
  // what each macro is on the node: its own value, what the agent found, or the default
  const effective = useResource<Record<string, { value: MacroValue; origin: "manual" | "auto" | "default" }>>(
    current && nodeId ? `/templates/macros?template_id=${current.id}&node_id=${nodeId}` : null,
  );

  const [instances, setInstances] = useState<Record<string, number[]> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // what the person typed, and the last result, belong to one template on one node: changing either
  // drops them, so the values start again from the node's own (or the defaults)
  const context = `${current?.id ?? ""}|${nodeId}`;
  const [typed, setTyped] = useState<{ context: string; values: Record<string, string> }>({ context, values: {} });
  const [outcome, setOutcome] = useState<{ context: string; result: ApplyResult } | null>(null);
  const edits = typed.context === context ? typed.values : {};
  const result = outcome && outcome.context === context ? outcome.result : null;

  // the macro values: typed, else the node's own, else what the agent found, else the default
  const base = (name: string, fallback: MacroValue) => String(effective.data?.[name]?.value ?? fallback);
  const values: Record<string, string> = Object.fromEntries(
    (current?.macros ?? []).map((m) => [m.name, edits[m.name] ?? base(m.name, m.default)]),
  );

  const indexedColumns = (current?.items ?? [])
    .filter((i) => i.indexed)
    .map((i) => String(i.params.oid ?? "").replace(/^\./, ""));

  function overrides(): Record<string, string> {
    if (!current) return {};
    const out: Record<string, string> = {};
    for (const m of current.macros) {
      const v = values[m.name];
      if (v !== undefined && m.name in edits && v !== base(m.name, m.default)) out[m.name] = v;
    }
    return out;
  }

  async function readInstances() {
    setBusy(true);
    setError(null);
    const r = await api<Discovery>("/snmp-oids/discover", { method: "POST", body: { node_id: nodeId } });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    const found: Record<string, number[]> = {};
    for (const c of r.data.candidates) if (c.indexed) found[c.oid] = c.instances;
    setInstances(found);
  }

  async function run(dryRun: boolean) {
    if (!current) return;
    setBusy(true);
    setError(null);
    const r = await api<ApplyResult>(`/templates/${current.id}/apply`, {
      method: "POST",
      body: { node_id: nodeId, macros: overrides(), instances: instances ?? {}, dry_run: dryRun },
    });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    setOutcome({ context, result: r.data });
    if (!dryRun) {
      onApplied();
      effective.reload();
    }
  }

  return (
    <Modal title="Apply a template" onClose={onClose} wide>
      <div className="space-y-4">
        {error && <Alert>{error}</Alert>}
        <div className="grid grid-cols-2 gap-3">
          {!template && (
            <Field label="Template">
              <Select value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
                <option value="">Choose a template</option>
                {(list.data ?? []).map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name} ({t.category})
                  </option>
                ))}
              </Select>
            </Field>
          )}
          <Field label={capability ? `Node (with ${capability})` : "Node"}>
            <Select
              value={nodeId}
              onChange={(e) => {
                setNodeId(e.target.value);
                setInstances(null);
              }}
            >
              <option value="">Choose a node</option>
              {(nodes.data ?? []).map((n) => (
                <option key={n.id} value={n.id}>
                  {n.name}
                </option>
              ))}
            </Select>
          </Field>
        </div>

        {current && (
          <div className="space-y-2 rounded border border-line p-3">
            <p className="text-xs text-fg-muted">{current.description}</p>
            <table className="w-full text-left text-xs">
              <thead className="text-fg-muted">
                <tr>
                  <th className="py-1">Check</th>
                  <th className="py-1">Kind</th>
                  <th className="py-1">Thresholds</th>
                </tr>
              </thead>
              <tbody>
                {current.items.map((item) => (
                  <tr key={item.name} className="border-t border-line">
                    <td className="py-1">{item.name}{item.indexed ? " (per instance)" : ""}</td>
                    <td className="py-1 font-mono text-[11px]">{item.kind}</td>
                    <td className="py-1">{thresholds(item)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {current && current.macros.length > 0 && (
          <div className="space-y-2">
            <div className="text-xs font-semibold uppercase tracking-wider text-fg-muted">Macros</div>
            <div className="grid grid-cols-2 gap-3">
              {current.macros.map((m) => (
                <Field
                  key={m.name}
                  label={`{$${m.name}}${originLabel(m.name in edits ? "manual" : effective.data?.[m.name]?.origin)}`}
                  hint={m.description}
                >
                  <TextInput
                    value={values[m.name] ?? ""}
                    onChange={(e) => setTyped({ context, values: { ...edits, [m.name]: e.target.value } })}
                  />
                </Field>
              ))}
            </div>
          </div>
        )}

        {indexedColumns.length > 0 && (
          <div className="space-y-2">
            <Button type="button" onClick={() => void readInstances()} disabled={busy || !nodeId}>
              Read instances from the node
            </Button>
            {instances && (
              <div className="text-xs text-fg-muted">
                {Object.keys(instances).length === 0
                  ? "No table instances found: only the scalar checks will be created."
                  : Object.entries(instances).map(([oid, ids]) => (
                      <div key={oid}>
                        <span className="font-mono">{oid}</span>: instances {ids.join(", ")}
                      </div>
                    ))}
              </div>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button type="button" onClick={() => void run(true)} disabled={busy || !nodeId || !current}>
            Preview
          </Button>
          <Button type="button" variant="primary" onClick={() => void run(false)} disabled={busy || !nodeId || !current}>
            {busy ? "Working…" : "Apply to node"}
          </Button>
        </div>

        {result && (
          <div className="space-y-2 rounded border border-line p-3 text-xs">
            <div className={result.dry_run ? "text-fg-secondary" : "text-ok"}>
              {result.dry_run
                ? `Preview: ${result.created.length} checks would be created.`
                : `${result.created.length} checks created.`}
            </div>
            {result.created.length > 0 && <div>{result.created.join(", ")}</div>}
            {result.skipped.length > 0 && (
              <div className="space-y-1 text-fg-muted">
                <div>{result.skipped.length} skipped:</div>
                {result.skipped.map((s) => (
                  <div key={`${s.name}-${s.reason}`}>
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
