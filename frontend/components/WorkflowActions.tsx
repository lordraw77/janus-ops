"use client";

import { Activity, Bell, Box, Cog, Globe, HeartPulse, Plug, Plus, Server, ShieldCheck, Terminal, Timer } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui";
import { ACTION_GROUPS, ACTION_HINT, ACTION_LABEL, type Action } from "@/lib/workflows";

const cls = "h-3.5 w-3.5";
/** The icon of each kind of step. */
export const ACTION_ICON: Record<Action, ReactNode> = {
  exec: <Terminal className={cls} aria-hidden />,
  service: <Cog className={cls} aria-hidden />,
  process: <Activity className={cls} aria-hidden />,
  container: <Box className={cls} aria-hidden />,
  proxmox: <Server className={cls} aria-hidden />,
  http: <Globe className={cls} aria-hidden />,
  port: <Plug className={cls} aria-hidden />,
  check: <HeartPulse className={cls} aria-hidden />,
  wait: <Timer className={cls} aria-hidden />,
  notify: <Bell className={cls} aria-hidden />,
  approval: <ShieldCheck className={cls} aria-hidden />,
};

/** The colour of the title of each kind of step: what changes things, what only looks, the flow. */
export const ACTION_TONE: Record<Action, string> = {
  exec: "text-primary",
  service: "text-primary",
  process: "text-primary",
  container: "text-primary",
  proxmox: "text-primary",
  http: "text-telemetry",
  port: "text-telemetry",
  check: "text-telemetry",
  wait: "text-fg-secondary",
  notify: "text-ok",
  approval: "text-warn",
};

/** The "Add a step" bar, shared by the form editor and the graph editor. */
export function AddStepBar({ onAdd }: { onAdd: (action: Action) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
      <span className="flex items-center gap-1 text-xs text-fg-muted">
        <Plus className="h-3.5 w-3.5" aria-hidden /> Add a step:
      </span>
      {ACTION_GROUPS.map((g) => (
        <div key={g.title} className="flex flex-wrap items-center gap-1.5" role="group" aria-label={g.title}>
          <span className="text-[10px] font-semibold uppercase tracking-wider text-fg-muted">{g.title}</span>
          {g.actions.map((a) => (
            <Button key={a} onClick={() => onAdd(a)} title={ACTION_HINT[a]}>
              {ACTION_ICON[a]} {ACTION_LABEL[a]}
            </Button>
          ))}
        </div>
      ))}
    </div>
  );
}
