"use client";

import { Copy, Lock, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

import {
  type Detect,
  type Macro,
  type Template,
  type TemplateItem,
  type TemplateNode,
  TemplateApplyDialog,
  thresholds,
} from "@/components/TemplateApply";
import { Alert, Button, Card, Field, Modal, Select, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const CATEGORIES = ["proxmox", "database", "web", "server", "network", "nas", "ups", "generic"];
const REQUIRES = ["ssh", "agent", "proxmox", "docker", "snmp", "fritzbox", "postgres"];
const KINDS = ["host", "metric", "systemd", "service", "port", "tcp", "http", "backup", "snmp", "fritzbox", "probe"];
const CHECKBOX = "h-4 w-4 accent-primary";

/** What the editor changes: the body of POST/PUT /templates. */
type Draft = {
  name: string;
  category: string;
  description: string;
  requires: string | null;
  macros: Macro[];
  items: TemplateItem[];
  detect: Detect;
};

const EMPTY: Draft = {
  name: "",
  category: "generic",
  description: "",
  requires: null,
  macros: [],
  items: [{ name: "", kind: "tcp", params: { port: 22 }, interval_s: 60, check_cycles: 1 }],
  detect: { processes: [], ports: [], units: [] },
};

function draftOf(t: Template): Draft {
  return {
    name: t.name,
    category: t.category,
    description: t.description,
    requires: t.requires,
    macros: t.macros,
    items: t.items,
    detect: t.detect,
  };
}

function list(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** A number stays a number, `{$MACRO}` and anything else stays text. */
function threshold(value: string): number | string | null {
  const v = value.trim();
  if (v === "") return null;
  return Number.isFinite(Number(v)) ? Number(v) : v;
}

/** The params of one item as JSON text; the object is only replaced when the text parses. */
function ParamsInput({
  value,
  disabled,
  onChange,
}: {
  value: Record<string, unknown>;
  disabled: boolean;
  onChange: (v: Record<string, unknown>) => void;
}) {
  const [text, setText] = useState(JSON.stringify(value));
  const [valid, setValid] = useState(true);
  return (
    <TextInput
      value={text}
      disabled={disabled}
      aria-invalid={!valid}
      className={`font-mono text-xs ${valid ? "" : "border-crit"}`}
      onChange={(e) => {
        setText(e.target.value);
        try {
          const parsed: unknown = JSON.parse(e.target.value);
          if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not an object");
          setValid(true);
          onChange(parsed as Record<string, unknown>);
        } catch {
          setValid(false);
        }
      }}
    />
  );
}

function ItemsTab({
  draft,
  readOnly,
  set,
}: {
  draft: Draft;
  readOnly: boolean;
  set: (d: Draft) => void;
}) {
  const setItem = (i: number, patch: Partial<TemplateItem>) =>
    set({ ...draft, items: draft.items.map((it, n) => (n === i ? { ...it, ...patch } : it)) });
  return (
    <div className="space-y-3">
      {draft.items.map((item, i) => (
        <div key={i} className="space-y-2 rounded border border-line p-3">
          <div className="grid grid-cols-[1fr_9rem_6rem_6rem_auto] items-end gap-2">
            <Field label="Check name" hint="{$MACRO} allowed">
              <TextInput value={item.name} disabled={readOnly} onChange={(e) => setItem(i, { name: e.target.value })} />
            </Field>
            <Field label="Kind">
              <Select value={item.kind} disabled={readOnly} onChange={(e) => setItem(i, { kind: e.target.value })}>
                {KINDS.map((k) => (
                  <option key={k}>{k}</option>
                ))}
              </Select>
            </Field>
            <Field label="Every (s)">
              <TextInput
                type="number"
                min={10}
                value={item.interval_s ?? 60}
                disabled={readOnly}
                onChange={(e) => setItem(i, { interval_s: Number(e.target.value) })}
              />
            </Field>
            <Field label="Cycles">
              <TextInput
                type="number"
                min={1}
                value={item.check_cycles ?? 1}
                disabled={readOnly}
                onChange={(e) => setItem(i, { check_cycles: Number(e.target.value) })}
              />
            </Field>
            {!readOnly && (
              <Button
                type="button"
                variant="danger"
                aria-label="Remove the check"
                onClick={() => set({ ...draft, items: draft.items.filter((_, n) => n !== i) })}
              >
                <Trash2 className="h-3.5 w-3.5" aria-hidden />
              </Button>
            )}
          </div>
          <Field label="Parameters (JSON)" hint='e.g. {"port": "{$PG_PORT}"} or {"metric": "cpu.usage_pct"}'>
            <ParamsInput value={item.params} disabled={readOnly} onChange={(params) => setItem(i, { params })} />
          </Field>
          <div className="grid grid-cols-[8rem_1fr_1fr_auto] items-end gap-2">
            <Field label="Compare">
              <Select
                value={item.comparison ?? ""}
                disabled={readOnly}
                onChange={(e) =>
                  setItem(i, {
                    comparison: (e.target.value || null) as TemplateItem["comparison"],
                    ...(e.target.value ? {} : { warn: null, crit: null }),
                  })
                }
              >
                <option value="">none</option>
                <option value="gt">greater than</option>
                <option value="lt">less than</option>
              </Select>
            </Field>
            <Field label="Warning" hint="number or {$MACRO}">
              <TextInput
                value={item.warn ?? ""}
                disabled={readOnly || !item.comparison}
                onChange={(e) => setItem(i, { warn: threshold(e.target.value) })}
              />
            </Field>
            <Field label="Critical">
              <TextInput
                value={item.crit ?? ""}
                disabled={readOnly || !item.comparison}
                onChange={(e) => setItem(i, { crit: threshold(e.target.value) })}
              />
            </Field>
            {item.kind === "snmp" && (
              <label className="flex items-center gap-2 pb-2 text-xs">
                <input
                  type="checkbox"
                  className={CHECKBOX}
                  checked={!!item.indexed}
                  disabled={readOnly}
                  onChange={(e) => setItem(i, { indexed: e.target.checked })}
                />
                One per table instance
              </label>
            )}
          </div>
        </div>
      ))}
      {!readOnly && (
        <Button
          type="button"
          onClick={() =>
            set({ ...draft, items: [...draft.items, { name: "", kind: "tcp", params: { port: 22 }, interval_s: 60, check_cycles: 1 }] })
          }
        >
          <Plus className="h-3.5 w-3.5" aria-hidden /> Add a check
        </Button>
      )}
    </div>
  );
}

/** The discovery rule of a macro, in one line (the JSON tab edits it). */
function autoText(auto: Record<string, unknown>): string {
  const { from, ...rest } = auto;
  const detail = Object.entries(rest)
    .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join("|") : String(v)}`)
    .join(" ");
  return `${String(from)} ${detail}`.trim();
}

function MacrosTab({ draft, readOnly, set }: { draft: Draft; readOnly: boolean; set: (d: Draft) => void }) {
  const setMacro = (i: number, patch: Partial<Macro>) =>
    set({ ...draft, macros: draft.macros.map((m, n) => (n === i ? { ...m, ...patch } : m)) });
  return (
    <div className="space-y-3">
      <p className="text-xs text-fg-muted">
        A macro is a value the node can change: write <span className="font-mono">{"{$NAME}"}</span> in a check name,
        parameter or threshold. <span className="font-mono">{"{$NODE_ADDRESS}"}</span> and{" "}
        <span className="font-mono">{"{$NODE_NAME}"}</span> always exist.
      </p>
      {draft.macros.map((m, i) => (
        <div key={i} className="grid grid-cols-[12rem_10rem_1fr_auto] items-end gap-2">
          <Field label="Name" hint="A–Z, 0–9, _">
            <TextInput
              value={m.name}
              disabled={readOnly}
              className="font-mono"
              onChange={(e) => setMacro(i, { name: e.target.value.toUpperCase() })}
            />
          </Field>
          <Field label="Default">
            <TextInput
              value={String(m.default)}
              disabled={readOnly}
              onChange={(e) => {
                const v = e.target.value;
                setMacro(i, { default: v !== "" && Number.isFinite(Number(v)) ? Number(v) : v });
              }}
            />
          </Field>
          <Field label="Description">
            <TextInput value={m.description ?? ""} disabled={readOnly} onChange={(e) => setMacro(i, { description: e.target.value })} />
          </Field>
          {m.auto && (
            <div className="col-span-4 -mt-1 text-[11px] text-fg-muted">
              Found by the agent: <span className="font-mono">{autoText(m.auto)}</span>
            </div>
          )}
          {!readOnly && (
            <Button
              type="button"
              variant="danger"
              aria-label="Remove the macro"
              onClick={() => set({ ...draft, macros: draft.macros.filter((_, n) => n !== i) })}
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden />
            </Button>
          )}
        </div>
      ))}
      {!readOnly && (
        <Button type="button" onClick={() => set({ ...draft, macros: [...draft.macros, { name: "", default: "" }] })}>
          <Plus className="h-3.5 w-3.5" aria-hidden /> Add a macro
        </Button>
      )}
    </div>
  );
}

function DetectTab({ draft, readOnly, set }: { draft: Draft; readOnly: boolean; set: (d: Draft) => void }) {
  const d = draft.detect;
  return (
    <div className="space-y-3">
      <p className="text-xs text-fg-muted">
        What on a node makes this template worth suggesting: the agent&apos;s discovery matches these names (a trailing{" "}
        <span className="font-mono">*</span> matches any ending).
      </p>
      <Field label="Processes" hint="comma separated, e.g. postgres, postmaster">
        <TextInput
          defaultValue={d.processes.join(", ")}
          disabled={readOnly}
          onChange={(e) => set({ ...draft, detect: { ...d, processes: list(e.target.value) } })}
        />
      </Field>
      <Field label="Listening ports" hint="e.g. 5432">
        <TextInput
          defaultValue={d.ports.join(", ")}
          disabled={readOnly}
          onChange={(e) =>
            set({ ...draft, detect: { ...d, ports: list(e.target.value).map(Number).filter((n) => Number.isInteger(n)) } })
          }
        />
      </Field>
      <Field label="systemd units" hint="e.g. postgresql*">
        <TextInput
          defaultValue={d.units.join(", ")}
          disabled={readOnly}
          onChange={(e) => set({ ...draft, detect: { ...d, units: list(e.target.value) } })}
        />
      </Field>
    </div>
  );
}

function NodesTab({ template, onChanged }: { template: Template; onChanged: () => void }) {
  const { can } = useAuth();
  const nodes = useResource<TemplateNode[]>(`/templates/${template.id}/nodes`);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [applying, setApplying] = useState<string | null>(null);
  const manage = can("monitoring.manage");

  async function reapply(n: TemplateNode) {
    setError(null);
    const r = await api<{ created: string[] }>(`/templates/${template.id}/apply`, {
      method: "POST",
      body: { node_id: n.node_id },
    });
    if (!r.ok) return setError(message(r.code));
    setNotice(`${n.node_name}: ${r.data.created.length} checks added.`);
    nodes.reload();
    onChanged();
  }

  async function unlink(n: TemplateNode) {
    if (!window.confirm(`Stop tracking "${template.name}" on ${n.node_name}?`)) return;
    const remove =
      n.checks > 0 && window.confirm(`Also delete its ${n.checks} checks and their history?\nOK deletes them, Cancel keeps them.`);
    const r = await api(`/templates/${template.id}/nodes/${n.node_id}?remove_checks=${remove}`, { method: "DELETE" });
    if (!r.ok) return setError(message(r.code));
    nodes.reload();
    onChanged();
  }

  return (
    <div className="space-y-3">
      {error && <Alert>{error}</Alert>}
      {notice && <Alert kind="ok">{notice}</Alert>}
      {nodes.error && <Alert>{nodes.error}</Alert>}
      {nodes.data && nodes.data.length === 0 && <p className="text-xs text-fg-muted">Not applied to any node yet.</p>}
      {nodes.data && nodes.data.length > 0 && (
        <table className="w-full text-left text-xs">
          <thead className="text-fg-muted">
            <tr>
              <th className="py-1">Node</th>
              <th className="py-1">Checks</th>
              <th className="py-1">Macros</th>
              <th className="py-1">Applied</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody>
            {nodes.data.map((n) => (
              <tr key={n.node_id} className="border-t border-line align-top">
                <td className="py-1.5">
                  {n.node_name}
                  <div className="font-mono text-[11px] text-fg-muted">{n.address}</div>
                </td>
                <td className="py-1.5">{n.checks}</td>
                <td className="py-1.5 font-mono text-[11px]">
                  {Object.entries(n.macros).length === 0
                    ? "defaults"
                    : Object.entries(n.macros).map(([k, v]) => (
                        <div key={k}>
                          {k} = {String(v.value)} <span className="text-fg-muted">({v.origin})</span>
                        </div>
                      ))}
                </td>
                <td className="py-1.5">
                  v{n.applied_version} by {n.applied_by}
                  {n.update_available && (
                    <span className="ml-2 rounded bg-warn/15 px-1.5 py-0.5 text-[10px] font-semibold text-warn">
                      v{template.version} available
                    </span>
                  )}
                </td>
                <td className="py-1.5 text-right">
                  {manage && (
                    <div className="flex justify-end gap-1">
                      <Button type="button" onClick={() => setApplying(n.node_id)}>
                        Edit macros
                      </Button>
                      <Button type="button" onClick={() => void reapply(n)}>
                        Re-apply
                      </Button>
                      <Button type="button" variant="danger" aria-label="Stop tracking" onClick={() => void unlink(n)}>
                        <Trash2 className="h-3.5 w-3.5" aria-hidden />
                      </Button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {applying && (
        <TemplateApplyDialog
          template={template}
          nodeId={applying}
          onClose={() => setApplying(null)}
          onApplied={() => {
            nodes.reload();
            onChanged();
          }}
        />
      )}
    </div>
  );
}

type Tab = "items" | "macros" | "detect" | "json" | "nodes";

/** One template: read only for a built-in, editable for a manual one (or a new one). */
function TemplateDialog({
  template,
  onClose,
  onSaved,
}: {
  template: Template | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { can } = useAuth();
  const readOnly = !can("monitoring.manage") || template?.source === "built-in";
  const [draft, setDraft] = useState<Draft>(template ? draftOf(template) : EMPTY);
  const [tab, setTab] = useState<Tab>("items");
  const [json, setJson] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const tabs: Tab[] = template ? ["items", "macros", "detect", "json", "nodes"] : ["items", "macros", "detect", "json"];

  function openTab(next: Tab) {
    // the JSON tab edits the same draft: leaving it applies what was typed
    if (tab === "json" && next !== "json" && !applyJson()) return;
    if (next === "json") setJson(JSON.stringify(draft, null, 2));
    setTab(next);
  }

  function applyJson(): Draft | null {
    try {
      const parsed = JSON.parse(json) as Draft;
      const next: Draft = { ...EMPTY, ...parsed, detect: { ...EMPTY.detect, ...parsed.detect } };
      setDraft(next);
      setError(null);
      return next;
    } catch {
      setError("The JSON is not valid.");
      return null;
    }
  }

  async function save() {
    const body = tab === "json" ? applyJson() : draft;
    if (!body) return;
    setBusy(true);
    setError(null);
    const r = template
      ? await api<Template>(`/templates/${template.id}`, { method: "PUT", body })
      : await api<Template>("/templates", { method: "POST", body });
    setBusy(false);
    if (!r.ok) {
      if (r.status === 422) {
        // the server's reason for a rejected template
        return setError("The template is not valid: check the names, parameters, macros and thresholds.");
      }
      return setError(message(r.code));
    }
    onSaved();
    onClose();
  }

  return (
    <Modal title={template ? template.name : "New template"} onClose={onClose} wide>
      <div className="space-y-4">
        {error && <Alert>{error}</Alert>}
        {template?.source === "built-in" && (
          <Alert kind="info">
            Built-in templates are read only and updated with the product. Use “Duplicate” to make a copy you can change.
          </Alert>
        )}
        <div className="grid grid-cols-4 gap-3">
          <Field label="Name">
            <TextInput value={draft.name} disabled={readOnly} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
          </Field>
          <Field label="Category">
            <Select value={draft.category} disabled={readOnly} onChange={(e) => setDraft({ ...draft, category: e.target.value })}>
              {CATEGORIES.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </Select>
          </Field>
          <Field label="Needs on the node">
            <Select
              value={draft.requires ?? ""}
              disabled={readOnly}
              onChange={(e) => setDraft({ ...draft, requires: e.target.value || null })}
            >
              <option value="">nothing</option>
              {REQUIRES.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </Select>
          </Field>
          <Field label="Description">
            <TextInput
              value={draft.description}
              disabled={readOnly}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            />
          </Field>
        </div>

        <div role="tablist" className="flex gap-1 border-b border-line">
          {tabs.map((t) => (
            <button
              key={t}
              type="button"
              role="tab"
              aria-selected={tab === t}
              onClick={() => openTab(t)}
              className={`px-3 py-1.5 text-xs font-medium capitalize ${tab === t ? "border-b-2 border-primary text-fg" : "text-fg-muted hover:text-fg"}`}
            >
              {t === "json" ? "JSON" : t}
              {t === "items" && ` (${draft.items.length})`}
              {t === "macros" && ` (${draft.macros.length})`}
            </button>
          ))}
        </div>

        {tab === "items" && <ItemsTab draft={draft} readOnly={readOnly} set={setDraft} />}
        {tab === "macros" && <MacrosTab draft={draft} readOnly={readOnly} set={setDraft} />}
        {tab === "detect" && <DetectTab draft={draft} readOnly={readOnly} set={setDraft} />}
        {tab === "json" && (
          <textarea
            value={json}
            readOnly={readOnly}
            onChange={(e) => setJson(e.target.value)}
            spellCheck={false}
            aria-label="Template JSON"
            className="h-96 w-full rounded border border-line bg-canvas p-3 font-mono text-xs outline-none focus:border-primary"
          />
        )}
        {tab === "nodes" && template && <NodesTab template={template} onChanged={onSaved} />}

        {!readOnly && tab !== "nodes" && (
          <div className="flex justify-end gap-2">
            <Button type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="button" variant="primary" onClick={() => void save()} disabled={busy || !draft.name.trim()}>
              {busy ? "Saving…" : "Save"}
            </Button>
          </div>
        )}
      </div>
    </Modal>
  );
}

function DuplicateDialog({
  template,
  onClose,
  onDone,
}: {
  template: Template;
  onClose: () => void;
  onDone: (copy: Template) => void;
}) {
  const [name, setName] = useState(`${template.name} (copy)`);
  const [error, setError] = useState<string | null>(null);
  async function submit() {
    const r = await api<Template>(`/templates/${template.id}/duplicate`, { method: "POST", body: { name } });
    if (!r.ok) return setError(message(r.code));
    onDone(r.data);
  }
  return (
    <Modal title="Duplicate template" onClose={onClose}>
      <div className="space-y-3">
        {error && <Alert>{error}</Alert>}
        <Field label="Name of the copy">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <div className="flex justify-end">
          <Button type="button" variant="primary" disabled={!name.trim()} onClick={() => void submit()}>
            Duplicate
          </Button>
        </div>
      </div>
    </Modal>
  );
}

/** Monitoring templates: the list, the editor, and where each template is applied. */
export function TemplatesView() {
  const { can } = useAuth();
  const manage = can("monitoring.manage");
  const [category, setCategory] = useState("");
  const [search, setSearch] = useState("");
  const templates = useResource<Template[]>(`/templates${category ? `?category=${category}` : ""}`);
  const [open, setOpen] = useState<Template | "new" | null>(null);
  const [applying, setApplying] = useState<Template | null>(null);
  const [duplicating, setDuplicating] = useState<Template | null>(null);
  const [error, setError] = useState<string | null>(null);

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (templates.data ?? []).filter(
      (t) => !q || t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q),
    );
  }, [templates.data, search]);

  async function remove(t: Template) {
    const note = t.node_count ? ` It is applied to ${t.node_count} node(s): their checks stay.` : "";
    if (!window.confirm(`Delete the template "${t.name}"?${note}`)) return;
    const r = await api(`/templates/${t.id}`, { method: "DELETE" });
    if (!r.ok) return setError(message(r.code));
    templates.reload();
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Check templates</h1>
        {manage && (
          <Button type="button" variant="primary" onClick={() => setOpen("new")}>
            <Plus className="h-3.5 w-3.5" aria-hidden /> New template
          </Button>
        )}
      </div>
      <p className="max-w-3xl text-xs text-fg-muted">
        A template is a ready-made set of checks for a service or a device (Proxmox, PostgreSQL, Apache, a NAS…).
        Apply it to a node and its macros (port, unit, thresholds) are set for that node.
      </p>
      {error && <Alert>{error}</Alert>}
      {templates.error && <Alert>{templates.error}</Alert>}
      <Card>
        <div className="mb-3 flex gap-3">
          <div className="w-56">
            <Select value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Category">
              <option value="">All categories</option>
              {CATEGORIES.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </Select>
          </div>
          <div className="w-72">
            <TextInput placeholder="Search" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search" />
          </div>
        </div>
        <table className="w-full text-left text-xs">
          <thead className="text-fg-muted">
            <tr>
              <th className="py-1">Template</th>
              <th className="py-1">Category</th>
              <th className="py-1">Needs</th>
              <th className="py-1">Checks</th>
              <th className="py-1">Nodes</th>
              <th className="py-1">Version</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody>
            {shown.map((t) => (
              <tr key={t.id} className="border-t border-line align-top">
                <td className="py-2">
                  <button type="button" onClick={() => setOpen(t)} className="text-left font-medium hover:text-primary">
                    {t.name}
                  </button>
                  {t.source === "built-in" ? (
                    <span className="ml-2 inline-flex items-center gap-1 rounded bg-elevated px-1.5 py-0.5 text-[10px] text-fg-muted">
                      <Lock className="h-2.5 w-2.5" aria-hidden /> built-in
                    </span>
                  ) : (
                    <span className="ml-2 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">custom</span>
                  )}
                  <div className="max-w-xl text-[11px] text-fg-muted">{t.description}</div>
                  <div className="text-[11px] text-fg-muted">
                    {t.items.slice(0, 3).map((i) => `${i.name}${thresholds(i) ? ` (${thresholds(i)})` : ""}`).join(" · ")}
                    {t.items.length > 3 ? " …" : ""}
                  </div>
                </td>
                <td className="py-2">{t.category}</td>
                <td className="py-2">{t.requires ?? "—"}</td>
                <td className="py-2">{t.items.length}</td>
                <td className="py-2">{t.node_count ?? 0}</td>
                <td className="py-2">v{t.version}</td>
                <td className="py-2 text-right">
                  <div className="flex justify-end gap-1">
                    {manage && (
                      <Button type="button" onClick={() => setApplying(t)}>
                        <Play className="h-3.5 w-3.5" aria-hidden /> Apply
                      </Button>
                    )}
                    {manage && (
                      <Button type="button" aria-label={`Duplicate ${t.name}`} onClick={() => setDuplicating(t)}>
                        <Copy className="h-3.5 w-3.5" aria-hidden />
                      </Button>
                    )}
                    {manage && t.source === "manual" && (
                      <>
                        <Button type="button" aria-label={`Edit ${t.name}`} onClick={() => setOpen(t)}>
                          <Pencil className="h-3.5 w-3.5" aria-hidden />
                        </Button>
                        <Button type="button" variant="danger" aria-label={`Delete ${t.name}`} onClick={() => void remove(t)}>
                          <Trash2 className="h-3.5 w-3.5" aria-hidden />
                        </Button>
                      </>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {templates.data && shown.length === 0 && (
              <tr>
                <td colSpan={7} className="py-4 text-center text-fg-muted">
                  No template matches.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>

      {open && (
        <TemplateDialog
          template={open === "new" ? null : open}
          onClose={() => setOpen(null)}
          onSaved={templates.reload}
        />
      )}
      {applying && (
        <TemplateApplyDialog template={applying} onClose={() => setApplying(null)} onApplied={templates.reload} />
      )}
      {duplicating && (
        <DuplicateDialog
          template={duplicating}
          onClose={() => setDuplicating(null)}
          onDone={(copy) => {
            setDuplicating(null);
            templates.reload();
            setOpen(copy);
          }}
        />
      )}
    </div>
  );
}
