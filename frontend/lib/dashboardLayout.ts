/** How the Dashboard lays out its node cards: the three orders, the compact view and the saved choice.
 * Pure rules, no UI, so they are tested on their own (lib/dashboardLayout.test.mjs). */

import type { NodeCardData } from "./agents.ts";

export type SortMode = "name" | "load" | "custom";
export type Direction = "asc" | "desc";
export type Metric = "cpu" | "ram" | "disk" | "load";

export type Layout = {
  mode: SortMode;
  /** name: asc = A-Z. load: desc = the busiest first */
  dir: Direction;
  metric: Metric;
  compact: boolean;
  /** node ids in the order the user dragged them to */
  order: string[];
};

export const DEFAULT_LAYOUT: Layout = { mode: "name", dir: "asc", metric: "cpu", compact: false, order: [] };

export const METRICS: { id: Metric; label: string }[] = [
  { id: "cpu", label: "CPU" },
  { id: "ram", label: "RAM" },
  { id: "disk", label: "Disk" },
  { id: "load", label: "Load" },
];

/** The number a node is ranked by for a metric; null when the agent does not report it. */
export function metricOf(n: NodeCardData, metric: Metric): number | null {
  const v = metric === "cpu" ? n.latest.cpu_pct : metric === "ram" ? n.latest.mem_pct : metric === "disk" ? n.latest.disk_pct : n.latest.load1;
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** `envOrder` is the position of an environment in the settings; the default order groups by it. */
export function sortNodes(nodes: NodeCardData[], layout: Layout, envOrder: (env: string) => number): NodeCardData[] {
  const byName = (a: NodeCardData, b: NodeCardData) => a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true });
  const tidy = (a: NodeCardData, b: NodeCardData) =>
    Number(a.state === "unmonitored") - Number(b.state === "unmonitored") || envOrder(a.environment) - envOrder(b.environment) || byName(a, b);
  const list = [...nodes];
  if (layout.mode === "name") return list.sort((a, b) => (layout.dir === "asc" ? byName(a, b) : byName(b, a)) || a.node_id.localeCompare(b.node_id));
  if (layout.mode === "load") {
    const sign = layout.dir === "desc" ? -1 : 1;
    return list.sort((a, b) => {
      const x = metricOf(a, layout.metric);
      const y = metricOf(b, layout.metric);
      if (x === null || y === null) return Number(x === null) - Number(y === null) || byName(a, b); // no reading: last either way
      return sign * (x - y) || byName(a, b);
    });
  }
  // custom: the dragged order first; a node that is new (or was never placed) follows in the tidy order
  const rank = new Map(layout.order.map((id, i) => [id, i]));
  return list.sort((a, b) => {
    const x = rank.get(a.node_id);
    const y = rank.get(b.node_id);
    if (x !== undefined && y !== undefined) return x - y;
    if (x !== undefined) return -1;
    if (y !== undefined) return 1;
    return tidy(a, b);
  });
}

/** The order after dropping `id` where `target` is: just before it, or at the end when `target` is null. */
export function moveBefore(order: string[], id: string, target: string | null): string[] {
  if (id === target) return order;
  const rest = order.filter((x) => x !== id);
  const at = target === null ? -1 : rest.indexOf(target);
  if (at < 0) return [...rest, id];
  return [...rest.slice(0, at), id, ...rest.slice(at)];
}

const MODES: SortMode[] = ["name", "load", "custom"];
const METRIC_IDS = METRICS.map((m) => m.id);

/** What was saved, whatever shape it has now: anything not understood falls back to the default. */
export function readLayout(raw: string | null): Layout {
  if (!raw) return DEFAULT_LAYOUT;
  try {
    const v = JSON.parse(raw) as Partial<Layout> | null;
    if (!v || typeof v !== "object") return DEFAULT_LAYOUT;
    return {
      mode: MODES.includes(v.mode as SortMode) ? (v.mode as SortMode) : DEFAULT_LAYOUT.mode,
      dir: v.dir === "desc" ? "desc" : "asc",
      metric: METRIC_IDS.includes(v.metric as Metric) ? (v.metric as Metric) : DEFAULT_LAYOUT.metric,
      compact: v.compact === true,
      order: Array.isArray(v.order) ? v.order.filter((x): x is string => typeof x === "string").slice(0, 2000) : [],
    };
  } catch {
    return DEFAULT_LAYOUT;
  }
}

/** The label of the direction button for a mode. */
export function directionLabel(mode: SortMode, dir: Direction): string {
  if (mode === "load") return dir === "desc" ? "Highest first" : "Lowest first";
  return dir === "asc" ? "A → Z" : "Z → A";
}
