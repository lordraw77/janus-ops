"use client";

import {
  Boxes,
  Container,
  KeyRound,
  LayoutDashboard,
  LogOut,
  type LucideIcon,
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

import { RoleBadge } from "@/components/ui";
import { type Permission, useAuth } from "@/lib/auth";

type NavItem = {
  label: string;
  icon: LucideIcon;
  /** Route of an implemented page, or the roadmap phase that delivers it */
  /** permission: any of these grants the entry */
  target: { href: string; permission?: Permission | Permission[] } | { phase: string };
  ai?: boolean;
};

// Order follows the mockup (res/mockups/janus-ops-noc-preview.html) + the modules it lacked
const NAV: NavItem[] = [
  { label: "Dashboard", icon: LayoutDashboard, target: { href: "/" } },
  { label: "Inventory", icon: Server, target: { href: "/inventory/", permission: "inventory.read" } },
  { label: "Credentials", icon: KeyRound, target: { href: "/credentials/", permission: "inventory.read" } },
  { label: "Problems", icon: TriangleAlert, target: { phase: "F3" } },
  { label: "Containers", icon: Container, target: { phase: "F4" } },
  { label: "Proxmox VE", icon: Boxes, target: { phase: "F5" } },
  { label: "Ansible & Workflows", icon: Workflow, target: { phase: "F6" } },
  {
    label: "SSH Bastion",
    icon: SquareTerminal,
    target: { href: "/terminal/", permission: ["ssh.connect", "sftp.transfer", "audit.read"] },
  },
  { label: "Janus AI Copilot", icon: Sparkles, target: { phase: "F7" }, ai: true },
  { label: "Users & roles", icon: Users, target: { href: "/admin/users/", permission: "users.read" } },
  { label: "Audit log", icon: ScrollText, target: { href: "/audit/", permission: "audit.read" } },
  { label: "Settings", icon: Settings, target: { phase: "F0" } },
];

function isActive(pathname: string, href: string): boolean {
  const path = pathname.endsWith("/") ? pathname : `${pathname}/`;
  return href === "/" ? pathname === "/" : path.startsWith(href);
}

export function Sidebar() {
  const pathname = usePathname();
  const { user, can, logout } = useAuth();

  return (
    <aside className="flex w-56 shrink-0 flex-col justify-between border-r border-line bg-surface p-3">
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

        <nav aria-label="Main" className="space-y-1 text-xs">
          {NAV.map(({ label, icon: Icon, target, ai }) => {
            if ("href" in target) {
              const needed = target.permission === undefined ? [] : [target.permission].flat();
              if (needed.length && !needed.some((p) => can(p))) return null;
              const active = isActive(pathname, target.href);
              return (
                <Link
                  key={label}
                  href={target.href}
                  aria-current={active ? "page" : undefined}
                  className={`flex items-center gap-2 rounded border px-2.5 py-2 ${
                    active
                      ? "border-line bg-elevated text-primary"
                      : "border-transparent text-fg-nav hover:bg-elevated hover:text-fg"
                  }`}
                >
                  <Icon className="h-3.5 w-3.5" aria-hidden />
                  {label}
                </Link>
              );
            }
            return (
              <div
                key={label}
                aria-disabled="true"
                title={`Available in roadmap phase ${target.phase}`}
                className={`flex cursor-not-allowed items-center gap-2 rounded border border-transparent px-2.5 py-2 opacity-50 ${
                  ai ? "text-ai" : "text-fg-nav"
                }`}
              >
                <Icon className="h-3.5 w-3.5" aria-hidden />
                <span className="flex-1">{label}</span>
                <span className="font-mono text-[10px] text-fg-muted">{target.phase}</span>
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
