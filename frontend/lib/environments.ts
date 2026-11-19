"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

/** A preset name from before custom colours (ok, warn, crit, primary, muted) or #rrggbb; see envColor.ts. */
export type EnvColor = string;
export type Environment = { name: string; label: string; color: EnvColor | null; position: number; nodes: number };

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
