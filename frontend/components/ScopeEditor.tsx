"use client";

import { Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui";
import { useResource } from "@/lib/api";
import { useEnvironments } from "@/lib/environments";
import { CAPABILITIES, type CapabilityKind, type Group, type InventoryNode, type Tag } from "@/lib/inventory";

export type ScopeRule = {
  environments: string[];
  tags: string[];
  groups: string[];
  capabilities: CapabilityKind[];
  nodes: string[];
};
export type NodeScope = "all" | "restricted";

export const EMPTY_RULE: ScopeRule = { environments: [], tags: [], groups: [], capabilities: [], nodes: [] };

function Chips<T extends string>({
  label,
  options,
  value,
  onChange,
  empty,
}: {
  label: string;
  options: { id: T; text: string }[];
  value: T[];
  onChange: (v: T[]) => void;
  empty: string;
}) {
  return (
    <div className="grid grid-cols-[6.5rem_1fr] items-start gap-2 text-xs">
      <span className="pt-1 text-fg-muted">{label}</span>
      <div className="flex flex-wrap gap-1">
        {options.length === 0 && <span className="py-1 text-fg-muted">{empty}</span>}
        {options.map((o) => {
          const on = value.includes(o.id);
          return (
            <button
              key={o.id}
              type="button"
              aria-pressed={on}
              onClick={() => onChange(on ? value.filter((v) => v !== o.id) : [...value, o.id])}
              className={`rounded border px-1.5 py-0.5 font-mono text-[11px] ${on ? "border-primary bg-primary/15 text-primary" : "border-line text-fg-secondary hover:border-line-strong"}`}
            >
              {o.text}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** What a rule means, in words (also used in the roles table). */
export function describeRule(
  r: ScopeRule,
  lookup: { tags: Record<string, string>; groups: Record<string, string>; nodes: Record<string, string> },
): string {
  const parts = [
    r.environments.length ? `environment ${r.environments.join(" or ")}` : "",
    r.tags.length ? `tag ${r.tags.map((t) => lookup.tags[t] ?? "?").join(" or ")}` : "",
    r.groups.length ? `group ${r.groups.map((g) => lookup.groups[g] ?? "?").join(" or ")}` : "",
    r.capabilities.length ? `${r.capabilities.join(" or ")} capability` : "",
    r.nodes.length ? `node ${r.nodes.map((n) => lookup.nodes[n] ?? "?").join(" or ")}` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" and ") : "every node";
}

export function ScopeEditor({
  scope,
  rules,
  onScope,
  onRules,
}: {
  scope: NodeScope;
  rules: ScopeRule[];
  onScope: (s: NodeScope) => void;
  onRules: (r: ScopeRule[]) => void;
}) {
  const { list: environments } = useEnvironments();
  const tags = useResource<Tag[]>("/tags");
  const groups = useResource<Group[]>("/groups");
  const nodes = useResource<InventoryNode[]>("/nodes");
  const lookup = {
    tags: Object.fromEntries((tags.data ?? []).map((t) => [t.id, t.name])),
    groups: Object.fromEntries((groups.data ?? []).map((g) => [g.id, g.name])),
    nodes: Object.fromEntries((nodes.data ?? []).map((n) => [n.id, n.name])),
  };
  const set = (i: number, patch: Partial<ScopeRule>) => onRules(rules.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <fieldset className="space-y-3 rounded border border-line p-3">
      <legend className="px-1 text-xs font-medium text-fg-secondary">Nodes this role can see</legend>
      <div className="flex gap-4 text-xs">
        {(["all", "restricted"] as const).map((v) => (
          <label key={v} className="flex items-center gap-1.5">
            <input type="radio" name="node-scope" checked={scope === v} onChange={() => onScope(v)} className="accent-primary" />
            {v === "all" ? "All nodes" : "Only the nodes matching a rule"}
          </label>
        ))}
      </div>
      {scope === "restricted" && (
        <div className="space-y-2">
          <p className="text-[11px] text-fg-muted">
            A node is visible when it matches any rule; inside a rule every filled-in line must match.
            Lines left empty do not filter. This covers the inventory, checks, problems, terminals, files, containers and
            Proxmox hosts. Group downtimes stay with roles that see every node.
          </p>
          {rules.length === 0 && <p className="text-xs text-warn">No rule: this role sees no node at all.</p>}
          {rules.map((rule, i) => (
            <div key={i} className="space-y-1.5 rounded border border-line bg-canvas p-2">
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-semibold uppercase tracking-wider text-fg-muted">Rule {i + 1}</span>
                <button type="button" onClick={() => onRules(rules.filter((_, j) => j !== i))} aria-label={`Remove rule ${i + 1}`} className="text-fg-muted hover:text-crit">
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                </button>
              </div>
              <Chips label="Environment" empty="none defined" value={rule.environments} onChange={(v) => set(i, { environments: v })} options={environments.map((e) => ({ id: e.name, text: e.name }))} />
              <Chips label="Capability" empty="" value={rule.capabilities} onChange={(v) => set(i, { capabilities: v })} options={(Object.keys(CAPABILITIES) as CapabilityKind[]).map((k) => ({ id: k, text: k }))} />
              <Chips label="Tag" empty="no tags yet" value={rule.tags} onChange={(v) => set(i, { tags: v })} options={(tags.data ?? []).map((t) => ({ id: t.id, text: `#${t.name}` }))} />
              <Chips label="Group" empty="no groups yet" value={rule.groups} onChange={(v) => set(i, { groups: v })} options={(groups.data ?? []).map((g) => ({ id: g.id, text: g.name }))} />
              <details>
                <summary className="cursor-pointer text-xs text-fg-muted">Specific nodes{rule.nodes.length ? ` (${rule.nodes.length})` : ""}</summary>
                <div className="mt-1 max-h-32 overflow-y-auto">
                  <Chips label="" empty="no nodes" value={rule.nodes} onChange={(v) => set(i, { nodes: v })} options={(nodes.data ?? []).map((n) => ({ id: n.id, text: n.name }))} />
                </div>
              </details>
              <p className="text-[11px] text-fg-secondary">→ {describeRule(rule, lookup)}</p>
            </div>
          ))}
          <Button onClick={() => onRules([...rules, { ...EMPTY_RULE }])}>
            <Plus className="h-3 w-3" aria-hidden /> Add rule
          </Button>
        </div>
      )}
    </fieldset>
  );
}
