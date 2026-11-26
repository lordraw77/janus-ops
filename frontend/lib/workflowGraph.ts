/** The graph view of a workflow definition (FR-80): how a definition becomes nodes and edges, and how
 * a gesture on the canvas (drag, connect, remove) becomes a change to the definition. Pure functions,
 * no UI: the canvas component only draws what `toGraph` says and calls these on user gestures, and
 * the definition stays the single source of truth (what the API stores and what a run executes).
 *
 * The model has no edges of its own. A run goes through the steps in order, a step may carry one
 * `when` condition on an earlier step. So the graph shows two kinds of edge: the order (derived, from
 * each step to the next) and the branches (one per `when`, from the step it looks at to the step it
 * guards). Order is "top to bottom": dragging a node past another one changes the order. */

import { type Action, type Definition, type Outcome, type Pos, type Step, defaultParams, freeId } from "./workflows.ts";

export const TRIGGER_ID = "__trigger__";
export const ROW_H = 150;
export const LANE_X = 320;

export type FlowNode = {
  id: string;
  type: "trigger" | "step";
  position: Pos;
  data: { stepId: string; index: number; step?: Step; trigger?: Definition["trigger"]; status?: string; issue?: boolean };
};
export type FlowEdge = {
  id: string;
  type: "seq" | "branch";
  source: string;
  target: string;
  sourceHandle?: string;
  targetHandle?: string;
  data?: { outcome: Outcome; contains?: string };
};

/** Where a step is drawn when the editor has not been told: a column, branches in a lane to the right. */
export function autoPosition(def: Definition, index: number): Pos {
  return { x: def.steps[index].when ? LANE_X : 0, y: (index + 1) * ROW_H };
}

export function positionOf(def: Definition, index: number): Pos {
  return def.steps[index].ui ?? autoPosition(def, index);
}

export function toGraph(def: Definition, statuses: Record<string, string> = {}, issues: Set<string> = new Set()): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const nodes: FlowNode[] = [
    { id: TRIGGER_ID, type: "trigger", position: { x: 0, y: 0 }, data: { stepId: TRIGGER_ID, index: -1, trigger: def.trigger } },
    ...def.steps.map((s, i): FlowNode => ({
      id: s.id,
      type: "step",
      position: positionOf(def, i),
      data: { stepId: s.id, index: i, step: s, status: statuses[s.id], issue: issues.has(s.id) },
    })),
  ];
  const edges: FlowEdge[] = [];
  let previous = TRIGGER_ID;
  for (const s of def.steps) {
    edges.push({ id: `seq:${previous}>${s.id}`, type: "seq", source: previous, target: s.id });
    previous = s.id;
  }
  for (const s of def.steps) {
    if (s.when && def.steps.some((o) => o.id === s.when?.step)) {
      edges.push({
        id: `branch:${s.when.step}>${s.id}`,
        type: "branch",
        source: s.when.step,
        target: s.id,
        sourceHandle: "branch",
        targetHandle: "cond",
        data: { outcome: s.when.outcome, contains: s.when.contains },
      });
    }
  }
  return { nodes, edges };
}

const round = (p: Pos): Pos => ({ x: Math.round(p.x), y: Math.round(p.y) });

/** Give every step an explicit position (the one it is drawn at now), so that moving one step does
 * not make the others jump when the automatic layout would shift. */
export function freezeLayout(def: Definition): Definition {
  return { ...def, steps: def.steps.map((s, i) => ({ ...s, ui: round(positionOf(def, i)) })) };
}

export function moveStep(def: Definition, id: string, pos: Pos): Definition {
  return { ...def, steps: def.steps.map((s) => (s.id === id ? { ...s, ui: round(pos) } : s)) };
}

/** The run order follows the vertical position: sort by y (stable, so ties keep their order). */
export function orderByPosition(def: Definition): Definition {
  const keyed = def.steps.map((s, i) => ({ s, i, y: positionOf(def, i).y }));
  keyed.sort((a, b) => a.y - b.y || a.i - b.i);
  const steps = keyed.map((k) => k.s);
  return steps.every((s, i) => s === def.steps[i]) ? def : { ...def, steps };
}

/** A drag ended: the step now sits at `pos`; the order follows. */
export function dropStep(def: Definition, id: string, pos: Pos): Definition {
  return orderByPosition(moveStep(freezeLayout(def), id, pos));
}

/** Lay the steps out as a column again, keeping each one's sideways position. */
export function relayout(def: Definition): Definition {
  return { ...def, steps: def.steps.map((s, i) => ({ ...s, ui: { x: s.ui?.x ?? autoPosition(def, i).x, y: (i + 1) * ROW_H } })) };
}

export type Connected = { def: Definition; note?: string };

/** Draw a branch: `target` runs only if `source` ended with `outcome`. A step has at most one branch,
 * so a new one replaces the old. If `source` comes after `target`, the target moves right below it
 * (a branch always looks back). */
export function connect(def: Definition, source: string, target: string, outcome: Outcome = "failed"): Connected {
  const si = def.steps.findIndex((s) => s.id === source);
  const ti = def.steps.findIndex((s) => s.id === target);
  if (si < 0 || ti < 0 || source === target) return { def };
  let steps = def.steps.map((s) => (s.id === target ? { ...s, when: { step: source, outcome } } : s));
  let note: string | undefined;
  if (si > ti) {
    const moved = steps[ti];
    steps = steps.filter((_, k) => k !== ti);
    const at = steps.findIndex((s) => s.id === source) + 1;
    steps.splice(at, 0, moved);
    note = `${target} now comes after ${source}, so the branch points forward.`;
  }
  const next = { ...def, steps };
  return { def: note ? relayout(next) : next, note };
}

export function setOutcome(def: Definition, target: string, outcome: Outcome): Definition {
  return { ...def, steps: def.steps.map((s) => (s.id === target && s.when ? { ...s, when: { ...s.when, outcome } } : s)) };
}

/** Rename a step; the branches that looked at it follow. */
export function renameStep(def: Definition, id: string, next: string): Definition {
  return {
    ...def,
    steps: def.steps.map((s) => {
      if (s.id === id) return { ...s, id: next };
      return s.when?.step === id ? { ...s, when: { ...s.when, step: next } } : s;
    }),
  };
}

/** Set (or clear, with null) the condition of a step. */
export function setWhen(def: Definition, id: string, when: Step["when"] | null): Definition {
  return { ...def, steps: def.steps.map((s) => (s.id === id ? { ...s, when: when ?? undefined } : s)) };
}

export function removeBranch(def: Definition, target: string): Definition {
  return { ...def, steps: def.steps.map((s) => (s.id === target ? { ...s, when: undefined } : s)) };
}

export function removeStep(def: Definition, id: string): Definition {
  // a branch that looked at the removed step goes with it
  return { ...def, steps: def.steps.filter((s) => s.id !== id).map((s) => (s.when?.step === id ? { ...s, when: undefined } : s)) };
}

/** A new step below the last one. */
export function addStep(def: Definition, action: Action): Definition {
  const step: Step = { id: freeId(def.steps), action, params: defaultParams(action) };
  const last = def.steps.length;
  step.ui = round({ x: 0, y: (last + 1) * ROW_H });
  return { ...def, steps: [...def.steps, step] };
}

/** One line saying what a step does, for the node. */
export function describeStep(step: Step): string {
  const p = step.params;
  const op = String(p.op ?? "");
  switch (step.action) {
    case "exec":
      return String(p.command ?? "") || "no command yet";
    case "notify":
      return String(p.message ?? "") || "no message yet";
    case "service":
    case "container":
      return p.name ? `${op} ${p.name}` : "no name yet";
    case "process":
      return p.name ? `${op === "kill" ? `kill (${p.signal})` : "check"} ${p.name}` : "no name yet";
    case "http":
      return `${p.method ?? "GET"} ${p.scheme ?? "http"}://…:${p.port ?? 80}${p.path ?? "/"} → ${p.expect_status ?? 200}`;
    case "port":
      return `port ${p.port} is ${p.expect ?? "open"}`;
    case "wait":
      return `wait ${p.seconds} s`;
    case "check":
      return p.check_id ? `check is ${String(p.state ?? "ok").toUpperCase()}${Number(p.wait_s) ? ` (wait ${p.wait_s} s)` : ""}` : "no check yet";
    case "proxmox":
      return `${op || "status"} ${p.kind} ${p.vmid}${p.pve_node ? ` on ${p.pve_node}` : ""}`;
    default:
      return String(p.reason ?? "") || "waits for a person";
  }
}

export const STATUS_TONE: Record<string, string> = {
  done: "border-ok text-ok",
  failed: "border-crit text-crit",
  skipped: "border-line text-fg-muted",
  running: "border-primary text-primary",
  waiting_approval: "border-warn text-warn",
  pending: "border-line text-fg-muted",
};
