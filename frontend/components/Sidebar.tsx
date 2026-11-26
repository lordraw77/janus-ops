"use client";

import {
  Archive,
  ChevronDown,
  ChevronRight,
  Bell,
  Bot,
  RadioTower,
  Boxes,
  GitBranch,
  GitFork,
  Container,
  KeyRound,
  LayoutDashboard,
  LayoutTemplate,
  LogOut,
  type LucideIcon,
  Search,
  ScrollText,
  Server,
  Settings,
  Sparkles,
  SquareTerminal,
  TriangleAlert,
  UserCog,
  Users,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { OPEN_PALETTE } from "@/components/CommandPalette";
import { RoleBadge } from "@/components/ui";
import { type Permission, useAuth } from "@/lib/auth";

type NavItem = {
  label: string;
  icon: LucideIcon;
  /** Route of an implemented page, or the roadmap phase that delivers it */
  /** permission: any of these grants the entry */
  target: { href: string; permission?: Permission | Permission[] } | { phase: string };
  ai?: boolean;
  /** extra words the command palette (Ctrl+K) also matches */
  keywords?: string;
};

// Order follows the mockup (res/mockups/janus-ops-noc-preview.html) + the modules it lacked
export const NAV: NavItem[] = [
  { label: "Dashboard", icon: LayoutDashboard, target: { href: "/" } },
  { label: "Inventory", icon: Server, target: { href: "/inventory/", permission: "inventory.read" } },
  { label: "Credentials", icon: KeyRound, target: { href: "/credentials/", permission: "inventory.read" } },
  { label: "Templates", icon: LayoutTemplate, keywords: "checks monitoring postgres proxmox apache zabbix", target: { href: "/templates/", permission: "inventory.read" } },
  { label: "Problems", icon: TriangleAlert, target: { href: "/problems/", permission: "inventory.read" } },
  { label: "SNMP traps", icon: RadioTower, keywords: "mib notifications snmp", target: { href: "/traps/", permission: "inventory.read" } },
  { label: "Containers", icon: Container, target: { href: "/containers/", permission: "inventory.read" } },
  { label: "Proxmox VE", icon: Boxes, target: { href: "/proxmox/", permission: "inventory.read" } },
  { label: "Ansible", icon: Workflow, keywords: "playbook", target: { href: "/ansible/", permission: "ansible.read" } },
  { label: "Workflows", icon: GitBranch, target: { href: "/workflows/", permission: "inventory.read" } },
  { label: "GitOps", icon: GitFork, target: { href: "/gitops/", permission: "inventory.read" } },
  {
    label: "SSH Bastion",
    icon: SquareTerminal,
    keywords: "terminal shell sftp files",
    target: { href: "/terminal/", permission: ["ssh.connect", "sftp.transfer", "audit.read"] },
  },
  { label: "Janus AI Copilot", icon: Sparkles, keywords: "ask diagnose ai", target: { href: "/ai/", permission: "ai.use" }, ai: true },
  { label: "Operate", icon: Bot, keywords: "chat ai change configure ntp servers", target: { href: "/operate/", permission: "ai.operate" }, ai: true },
  { label: "Users & roles", icon: Users, target: { href: "/admin/users/", permission: "users.read" } },
  { label: "Archive", icon: Archive, keywords: "recordings sessions chats old retention", target: { href: "/archive/", permission: "archive.read" } },
  { label: "Audit log", icon: ScrollText, keywords: "history who did", target: { href: "/audit/", permission: "audit.read" } },
  { label: "Notifications", icon: Bell, target: { href: "/notifications/", permission: "notifications.manage" } },
  { label: "Settings", icon: Settings, target: { href: "/settings/", permission: "inventory.read" } },
];

/** The menu by area. Items are found by label in NAV, which also feeds the Ctrl+K palette. */
const GROUPS: { id: string; label: string; items: string[] }[] = [
  { id: "infra", label: "Infrastructure", items: ["Inventory", "Credentials", "Templates", "Containers", "Proxmox VE"] },
  { id: "monitoring", label: "Monitoring", items: ["Problems", "SNMP traps", "Notifications"] },
  { id: "automation", label: "Automation", items: ["Ansible", "Workflows", "GitOps", "SSH Bastion"] },
  { id: "ai", label: "AI", items: ["Janus AI Copilot", "Operate"] },
  { id: "admin", label: "Administration", items: ["Users & roles", "Audit log", "Archive", "Settings"] },
];
const COLLAPSED_KEY = "janus.nav.collapsed";

function isActive(pathname: string, href: string): boolean {
  const path = pathname.endsWith("/") ? pathname : `${pathname}/`;
  return href === "/" ? pathname === "/" : path.startsWith(href);
}

export function Sidebar() {
  const pathname = usePathname();
  const { user, can, logout } = useAuth();
  const [collapsed, setCollapsed] = useState<string[]>([]);

  // which areas are folded is a per-browser convenience
  useEffect(() => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(COLLAPSED_KEY) ?? "[]") as unknown;
      // eslint-disable-next-line react-hooks/set-state-in-effect
      if (Array.isArray(saved)) setCollapsed(saved.filter((x): x is string => typeof x === "string"));
    } catch {
      /* storage blocked or unreadable: everything stays open */
    }
  }, []);

  function toggle(id: string) {
    setCollapsed((cur) => {
      const next = cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
      try {
        window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify(next));
      } catch {
        /* not persisted */
      }
      return next;
    });
  }

  const allowed = (item: NavItem): boolean => {
    if (!("href" in item.target)) return false;
    const needed = item.target.permission === undefined ? [] : [item.target.permission].flat();
    return needed.length === 0 || needed.some((p) => can(p));
  };

  function renderItem(item: NavItem) {
    if (!("href" in item.target)) {
      return (
        <div
          key={item.label}
          aria-disabled="true"
          title={`Available in roadmap phase ${item.target.phase}`}
          className={`flex cursor-not-allowed items-center gap-2 rounded border border-transparent px-2.5 py-2 opacity-50 ${item.ai ? "text-ai" : "text-fg-nav"}`}
        >
          <item.icon className="h-3.5 w-3.5" aria-hidden />
          <span className="flex-1">{item.label}</span>
          <span className="font-mono text-[10px] text-fg-muted">{item.target.phase}</span>
        </div>
      );
    }
    const active = isActive(pathname, item.target.href);
    return (
      <Link
        key={item.label}
        href={item.target.href}
        aria-current={active ? "page" : undefined}
        className={`flex items-center gap-2 rounded-md border px-2.5 py-1.5 transition-colors ${
          active
            ? "border-line bg-elevated text-primary shadow-[inset_2px_0_0_var(--color-primary)]"
            : `border-transparent hover:bg-elevated hover:text-fg ${item.ai ? "text-ai/80 hover:text-ai" : "text-fg-nav"}`
        }`}
      >
        <item.icon className="h-3.5 w-3.5" aria-hidden />
        {item.label}
      </Link>
    );
  }

  const byLabel = new Map(NAV.map((n) => [n.label, n]));
  const grouped = new Set(GROUPS.flatMap((g) => g.items));
  const top = NAV.filter((n) => !grouped.has(n.label) && allowed(n));

  return (
    <aside className="sticky top-0 flex h-screen w-56 shrink-0 flex-col justify-between self-start overflow-y-auto border-r border-line bg-surface p-3">
      <div>
        <div className="mb-4 flex items-center gap-2.5 border-b border-line px-2 py-2">
          {/* eslint-disable-next-line @next/next/no-img-element -- static export, no optimizer */}
          <img src="/brand/janus-ops-logo-256.png" alt="" className="h-10 w-10 object-contain" />
          <div>
            <div className="text-sm font-bold">
              Janus-<span className="text-primary">Ops</span>
            </div>
            <div className="font-mono text-[10px] text-fg-muted">NOC CONTROL PLANE</div>
          </div>
        </div>

        <button
          type="button"
          onClick={() => window.dispatchEvent(new Event(OPEN_PALETTE))}
          className="mb-3 flex w-full items-center justify-between rounded-md border border-line bg-canvas/40 px-2.5 py-1.5 text-xs text-fg-muted transition-colors hover:border-line-strong hover:text-fg"
        >
          <span className="flex items-center gap-2">
            <Search className="h-3.5 w-3.5" aria-hidden /> Search…
          </span>
          <kbd className="rounded border border-line bg-elevated px-1 font-mono text-[10px]">Ctrl K</kbd>
        </button>

        <nav aria-label="Main" className="space-y-1 text-xs">
          {top.map(renderItem)}
          {GROUPS.map((g) => {
            const items = g.items.map((l) => byLabel.get(l)).filter((n): n is NavItem => !!n && allowed(n));
            if (items.length === 0) return null;
            const open = !collapsed.includes(g.id);
            const current = items.find((n) => "href" in n.target && isActive(pathname, n.target.href));
            return (
              <div key={g.id} className="pt-1.5">
                <button
                  type="button"
                  onClick={() => toggle(g.id)}
                  aria-expanded={open}
                  className="flex w-full items-center gap-1 rounded px-1.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-fg-muted hover:text-fg"
                >
                  {open ? <ChevronDown className="h-3 w-3" aria-hidden /> : <ChevronRight className="h-3 w-3" aria-hidden />}
                  {g.label}
                  {!open && <span className="ml-auto font-mono text-[10px] normal-case tracking-normal">{items.length}</span>}
                </button>
                <div className="space-y-0.5">
                  {open ? items.map(renderItem) : current && renderItem(current)}
                </div>
              </div>
            );
          })}
        </nav>
      </div>

      <div className="space-y-2 border-t border-line px-2 pt-3">
        {user.method === "breakglass" && (
          <div className="rounded border border-warn/40 bg-warn/10 px-2 py-1 text-[11px] text-warn">
            Break-glass session
          </div>
        )}
        <div className="flex items-center justify-between gap-2">
          <Link href="/account/" className="min-w-0 rounded hover:text-primary" title="My account">
            <div className="flex items-center gap-1 truncate text-xs font-semibold">
              <UserCog className="h-3 w-3 shrink-0" aria-hidden />
              {user.display_name ?? user.username}
            </div>
            <div className="mt-0.5 flex flex-wrap gap-1">
              {user.roles.map((r) => (
                <RoleBadge key={r} role={r} />
              ))}
            </div>
          </Link>
          <button
            type="button"
            onClick={() => void logout()}
            title="Sign out"
            aria-label="Sign out"
            className="rounded border border-line bg-elevated p-1.5 text-fg-nav hover:text-fg"
          >
            <LogOut className="h-3.5 w-3.5" aria-hidden />
          </button>
        </div>
      </div>
    </aside>
  );
}
