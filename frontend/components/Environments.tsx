"use client";

import { Pencil, Plus, Trash2 } from "lucide-react";
import { type FormEvent, useState } from "react";

import { SortTh } from "@/components/SortTh";

import { EnvBadge } from "@/components/EnvBadge";
import { Alert, Button, Card, Field, Modal, TextInput } from "@/components/ui";
import { api, message } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { PRESET_COLORS, envHex, envStyle } from "@/lib/envColor";
import { type Environment, useEnvironments } from "@/lib/environments";
import { type Getters, useSortedRows } from "@/lib/sort";

function EnvironmentDialog({ env, onClose, onSaved }: { env: Environment | null; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(env?.name ?? "");
  const [label, setLabel] = useState(env?.label ?? "");
  // always #rrggbb in the form: an old preset name is shown as its colour
  const [color, setColor] = useState<string>(envHex(env?.color) ?? "");
  const [position, setPosition] = useState(String(env?.position ?? 100));
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const body = { label: label.trim(), color: color || null, position: Number(position) };
    const r = env
      ? await api(`/environments/${env.name}`, { method: "PATCH", body })
      : await api("/environments", { method: "POST", body: { name: name.trim(), ...body } });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }

  return (
    <Modal title={env ? `Edit environment ${env.name}` : "New environment"} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        {!env && (
          <Field label="Name" hint="Lower-case letters, digits and - ; it cannot be changed later">
            <TextInput autoFocus required value={name} onChange={(e) => setName(e.target.value.toLowerCase())} className="font-mono" />
          </Field>
        )}
        <Field label="Label">
          <TextInput required value={label} onChange={(e) => setLabel(e.target.value)} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1">
            <span className="text-xs font-medium text-fg-secondary">Badge colour</span>
            <div className="flex flex-wrap items-center gap-1.5">
              <button
                type="button"
                onClick={() => setColor("")}
                aria-pressed={color === ""}
                title="Neutral"
                className={`h-6 rounded border px-1.5 text-[10px] text-fg-muted ${color === "" ? "border-fg" : "border-line"}`}
              >
                none
              </button>
              {PRESET_COLORS.map((c) => (
                <button
                  key={c.value}
                  type="button"
                  onClick={() => setColor(c.value)}
                  aria-pressed={color === c.value}
                  aria-label={c.label}
                  title={c.label}
                  style={{ backgroundColor: c.value }}
                  className={`h-6 w-6 rounded border-2 ${color === c.value ? "border-fg" : "border-transparent"}`}
                />
              ))}
              <label className="flex items-center gap-1 text-[10px] text-fg-muted" title="Any colour">
                custom
                <input
                  type="color"
                  value={color || "#64748b"}
                  onChange={(e) => setColor(e.target.value)}
                  className="h-6 w-8 cursor-pointer rounded border border-line bg-canvas p-0"
                />
              </label>
            </div>
            <div className="flex items-center gap-2 text-[11px] text-fg-muted">
              Preview
              <span
                style={envStyle(color)}
                className={`inline-block rounded border px-1.5 font-mono text-[10px] uppercase ${color ? "" : "border-line text-fg-muted"}`}
              >
                {name || env?.name || "env"}
              </span>
              <span className="font-mono">{color || "neutral"}</span>
            </div>
          </div>
          <Field label="Order" hint="Lower comes first">
            <TextInput type="number" min={0} max={10000} value={position} onChange={(e) => setPosition(e.target.value)} />
          </Field>
        </div>
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary">
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}

const ENV_SORT: Getters<Environment> = {
  name: (e) => e.name,
  label: (e) => e.label,
  order: (e) => e.position,
  nodes: (e) => e.nodes,
};

export function EnvironmentsCard() {
  const { can } = useAuth();
  const manage = can("environments.manage");
  const { list, reload } = useEnvironments();
  const { sorted, sort, toggle } = useSortedRows(list, ENV_SORT);
  const [editing, setEditing] = useState<Environment | "new" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function remove(e: Environment) {
    if (!window.confirm(`Delete the environment "${e.name}"?`)) return;
    const r = await api(`/environments/${e.name}`, { method: "DELETE" });
    setError(r.ok ? null : message(r.code));
    reload();
  }

  return (
    <Card
      title="Environments"
      actions={
        manage ? (
          <Button variant="primary" onClick={() => setEditing("new")}>
            <Plus className="h-3.5 w-3.5" aria-hidden /> New environment
          </Button>
        ) : null
      }
    >
      <p className="mb-3 text-xs text-fg-secondary">
        Every node belongs to one environment. Roles can be limited to some of them (Users &amp; roles), and notification
        rules filter by them. An environment in use cannot be deleted.
      </p>
      {error && <Alert>{error}</Alert>}
      <table className="w-full text-left text-xs">
        <thead className="border-b border-line text-fg-muted">
          <tr>
            <SortTh label="Name" col="name" sort={sort} onSort={toggle} />
            <SortTh label="Label" col="label" sort={sort} onSort={toggle} />
            <SortTh label="Order" col="order" sort={sort} onSort={toggle} />
            <SortTh label="Nodes" col="nodes" sort={sort} onSort={toggle} className="py-2 pr-3 text-right font-medium" />
            <th />
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {sorted.map((e) => (
            <tr key={e.name}>
              <td className="py-2 pr-3">
                <EnvBadge env={e.name} />
              </td>
              <td className="py-2 pr-3">{e.label}</td>
              <td className="py-2 pr-3 font-mono text-fg-muted">{e.position}</td>
              <td className="py-2 pr-3 text-right font-mono">{e.nodes}</td>
              <td className="py-2 text-right">
                {manage && (
                  <div className="flex justify-end gap-1">
                    <Button onClick={() => setEditing(e)} aria-label={`Edit ${e.name}`}>
                      <Pencil className="h-3 w-3" aria-hidden />
                    </Button>
                    <Button variant="danger" disabled={e.nodes > 0} title={e.nodes > 0 ? "In use by nodes" : undefined} onClick={() => void remove(e)} aria-label={`Delete ${e.name}`}>
                      <Trash2 className="h-3 w-3" aria-hidden />
                    </Button>
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {editing && (
        <EnvironmentDialog
          env={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
    </Card>
  );
}
