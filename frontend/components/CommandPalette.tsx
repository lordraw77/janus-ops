"use client";

import { Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { NAV } from "@/components/Sidebar";
import { useAuth } from "@/lib/auth";
import { api, message, useResource } from "@/lib/api";
import { type Entry, move, rank } from "@/lib/palette";

export const OPEN_PALETTE = "janus:palette";

type CheckRow = { id: string; name: string; node_name: string; enabled: boolean };
type ProblemRow = { id: string; node_name: string; check_name: string };
type NodeRow = { id: string; name: string; capabilities: { kind: string; enabled: boolean }[] };

/** Ctrl+K (or Cmd+K) from anywhere in the console: jump to a page or a node, open a terminal. */
export function CommandPalette() {
  const router = useRouter();
  const { can } = useAuth();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  // the nodes are read the first time the palette opens, not on every page
  const [wanted, setWanted] = useState(false);
  const nodes = useResource<NodeRow[]>(wanted && can("inventory.read") ? "/nodes" : null);
  const checks = useResource<CheckRow[]>(wanted && can("monitoring.manage") ? "/checks" : null);
  const problems = useResource<ProblemRow[]>(wanted && can("ai.use") ? "/problems?status=open&limit=100" : null);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    // opening always starts from an empty search
    const show = () => {
      setQuery("");
      setIndex(0);
      setNotice(null);
      setWanted(true);
      setOpen(true);
    };
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        show();
      }
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener(OPEN_PALETTE, show);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener(OPEN_PALETTE, show);
    };
  }, []);

  const entries = useMemo<Entry[]>(() => {
    const out: Entry[] = [];
    for (const item of NAV) {
      if (!("href" in item.target)) continue;
      const needed = item.target.permission === undefined ? [] : [item.target.permission].flat();
      if (needed.length && !needed.some((p) => can(p))) continue;
      out.push({ id: item.target.href, label: item.label, hint: item.target.href, href: item.target.href, group: "Go to", keywords: item.keywords });
    }
    out.push({ id: "/account/", label: "My account", hint: "/account/", href: "/account/", group: "Go to", keywords: "password totp two-factor profile" });
    for (const n of nodes.data ?? []) {
      out.push({ id: `n-${n.id}`, label: `Open ${n.name}`, hint: "Node", href: `/nodes/?id=${n.id}`, group: "Nodes", keywords: n.name });
      const ssh = n.capabilities.some((c) => c.kind === "ssh" && c.enabled);
      if (ssh && can("ssh.connect")) {
        out.push({ id: `t-${n.id}`, label: `Open SSH on ${n.name}`, hint: "Terminal", href: `/terminal/?node=${n.id}`, group: "Nodes", keywords: `${n.name} terminal shell` });
      }
      if (ssh && can("sftp.transfer")) {
        out.push({ id: `f-${n.id}`, label: `Browse files on ${n.name}`, hint: "SFTP", href: `/terminal/?node=${n.id}&view=files`, group: "Nodes", keywords: `${n.name} sftp upload download` });
      }
    }
    for (const c of checks.data ?? []) {
      if (!c.enabled) continue;
      out.push({ id: `c-${c.id}`, label: `Run check ${c.name} on ${c.node_name}`, hint: "Check now", group: "Actions", act: { kind: "run-check", checkId: c.id }, keywords: `${c.node_name} execute verify now` });
    }
    for (const p of problems.data ?? []) {
      out.push({ id: `p-${p.id}`, label: `Ask the AI about ${p.node_name} · ${p.check_name}`, hint: "Copilot", group: "Actions", href: `/ai/?problem=${p.id}`, keywords: "diagnose why down failing" });
    }
    return out;
  }, [can, nodes.data, checks.data, problems.data]);

  const shown = useMemo(() => rank(entries, query), [entries, query]);
  const current = Math.min(index, Math.max(shown.length - 1, 0));

  async function go(e: Entry | undefined) {
    if (!e) return;
    if (e.act) {
      const r = await api(`/checks/${e.act.checkId}/run`, { method: "POST" });
      setNotice(r.ok ? { ok: true, text: `Scheduled: ${e.label}. The result shows up in a few seconds.` } : { ok: false, text: message(r.code) });
      if (r.ok) window.setTimeout(() => setOpen(false), 1400);
      return;
    }
    setOpen(false);
    if (e.href) router.push(e.href);
  }

  if (!open) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
      className="fixed inset-0 z-[60] flex items-start justify-center bg-canvas/80 p-4 pt-[15vh]"
      onMouseDown={(e) => e.target === e.currentTarget && setOpen(false)}
      onKeyDown={(e) => {
        if (e.key === "Escape") setOpen(false);
        else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          setIndex(move(current, e.key === "ArrowDown" ? 1 : -1, shown.length));
        } else if (e.key === "Enter") {
          e.preventDefault();
          void go(shown[current]);
        }
      }}
    >
      <div className="w-full max-w-xl overflow-hidden rounded-lg border border-line bg-surface shadow-2xl">
        <div className="flex items-center gap-2 border-b border-line px-3">
          <Search className="h-4 w-4 text-fg-muted" aria-hidden />
          <input
            autoFocus
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-list"
            aria-activedescendant={shown[current] ? `palette-${shown[current].id}` : undefined}
            aria-label="Search pages and nodes"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setIndex(0);
            }}
            placeholder="Go to a page or a node, open an SSH terminal…"
            className="w-full bg-transparent py-3 text-sm outline-none placeholder:text-fg-muted"
          />
          <kbd className="rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-fg-muted">Esc</kbd>
        </div>
        <ul id="palette-list" role="listbox" className="max-h-[50vh] overflow-y-auto py-1">
          {shown.length === 0 && <li className="px-3 py-3 text-xs text-fg-muted">Nothing matches “{query}”.</li>}
          {shown.map((e, i) => (
            <li
              key={e.id}
              id={`palette-${e.id}`}
              role="option"
              aria-selected={i === current}
              onMouseMove={() => setIndex(i)}
              onClick={() => void go(e)}
              className={`flex cursor-pointer items-center justify-between gap-3 px-3 py-2 text-sm ${i === current ? "bg-elevated text-primary" : "text-fg"}`}
            >
              <span className="truncate">{e.label}</span>
              <span className="shrink-0 font-mono text-[10px] text-fg-muted">{e.hint}</span>
            </li>
          ))}
        </ul>
        {notice && (
          <p role="status" className={`border-t border-line px-3 py-1.5 text-[11px] ${notice.ok ? "text-ok" : "text-crit"}`}>
            {notice.text}
          </p>
        )}
        {nodes.error && <p className="border-t border-line px-3 py-1.5 text-[11px] text-warn">Nodes could not be listed: {nodes.error}</p>}
      </div>
    </div>
  );
}
