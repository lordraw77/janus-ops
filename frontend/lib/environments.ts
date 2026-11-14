"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

export type EnvColor = "ok" | "warn" | "crit" | "primary" | "muted";
export type Environment = { name: string; label: string; color: EnvColor | null; position: number; nodes: number };

export const ENV_COLORS: EnvColor[] = ["crit", "warn", "ok", "primary", "muted"];

const COLOR_CLASS: Record<EnvColor, string> = {
  ok: "border-ok/40 text-ok",
  warn: "border-warn/40 text-warn",
  crit: "border-crit/40 text-crit",
  primary: "border-primary/40 text-primary",
  muted: "border-line text-fg-muted",
};

export function envClass(color: EnvColor | null | undefined): string {
  return COLOR_CLASS[color ?? "muted"];
}

// One shared copy for the badges of a page: the list is short and changes rarely
let cache: Environment[] | null = null;
let inflight: Promise<void> | null = null;
const listeners = new Set<(list: Environment[]) => void>();

function load(force = false): Promise<void> {
  if (inflight && !force) return inflight;
  inflight = api<Environment[]>("/environments").then((r) => {
    inflight = null;
    if (r.ok) {
      cache = r.data;
      listeners.forEach((l) => l(r.data));
    }
  });
  return inflight;
}

/** The configured environments, ordered by position; `reload` after editing them. */
export function useEnvironments(): { list: Environment[]; names: string[]; reload: () => void } {
  const [list, setList] = useState<Environment[]>(cache ?? []);
  useEffect(() => {
    listeners.add(setList);
    void load(cache === null);
    return () => {
      listeners.delete(setList);
    };
  }, []);
  return { list, names: list.map((e) => e.name), reload: () => void load(true) };
}
