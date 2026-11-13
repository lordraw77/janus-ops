"use client";

import {
  Boxes,
  Container,
  LayoutDashboard,
  LogOut,
  type LucideIcon,
  ScrollText,
  Server,
  Settings,
  Sparkles,
  SquareTerminal,
  TriangleAlert,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useAuth } from "@/lib/auth";

const ROLE_STYLE: Record<string, string> = {
  admin: "border-primary/40 text-primary",
  operator: "border-telemetry/40 text-telemetry",
  auditor: "border-ai/40 text-ai",
};

type NavItem = {
  label: string;
  icon: LucideIcon;
  /** Route of an implemented page, or the roadmap phase that delivers it */
  target: { href: string } | { phase: string };
  ai?: boolean;
};

// Order follows the mockup (res/mockups/janus-ops-noc-preview.html) + the modules it lacked
const NAV: NavItem[] = [
  { label: "Dashboard", icon: LayoutDashboard, target: { href: "/" } },
  { label: "Inventory", icon: Server, target: { phase: "F0" } },
  { label: "Problems", icon: TriangleAlert, target: { phase: "F3" } },
  { label: "Containers", icon: Container, target: { phase: "F4" } },
  { label: "Proxmox VE", icon: Boxes, target: { phase: "F5" } },
  { label: "Ansible & Workflows", icon: Workflow, target: { phase: "F6" } },
  { label: "SSH Bastion", icon: SquareTerminal, target: { phase: "F1" } },
  { label: "Janus AI Copilot", icon: Sparkles, target: { phase: "F7" }, ai: true },
  { label: "Audit", icon: ScrollText, target: { phase: "F0" } },
  { label: "Settings", icon: Settings, target: { phase: "F0" } },
];

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

export function Sidebar() {
  const pathname = usePathname();
  const { user, logout } = useAuth();

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
          <div className="min-w-0">
            <div className="truncate text-xs font-semibold" title={user.username}>
              {user.display_name ?? user.username}
            </div>
            <span
              className={`mt-0.5 inline-block rounded border px-1.5 font-mono text-[10px] uppercase ${ROLE_STYLE[user.role] ?? ""}`}
            >
              {user.role}
            </span>
          </div>
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
