/** Workflow definitions as the API stores them (FR-80), and the pure rules the visual editor needs:
 * validation in words, normalisation into what the API accepts, lenient JSON reading. No UI here, so
 * it can be tested on its own (lib/workflows.test.mjs). */

/** The workflow definition the API stores: a trigger and an ordered list of steps (FR-80). */
export type TriggerType = "manual" | "interval" | "alert" | "webhook";
export type Trigger = { type: TriggerType; interval_s?: number; check_id?: string };
export type Action = "exec" | "notify" | "approval";
export type Outcome = "ok" | "failed" | "skipped";
export type When = { step: string; outcome: Outcome; contains?: string };
export type Pos = { x: number; y: number };
export type Step = {
  id: string;
  action: Action;
  params: Record<string, unknown>;
  when?: When;
  on_failure?: "fail" | "continue";
  /** where the graph editor draws the step: layout only, it never changes what a run does */
  ui?: Pos;
};
export type Definition = { trigger: Trigger; steps: Step[] };

export type NodeOption = { id: string; name: string; address: string };
export type ChannelOption = { id: string; name: string; kind: string; enabled: boolean };
export type CheckOption = { id: string; name: string; node_name: string };

const ID_RE = /^[a-z][a-z0-9_-]{0,31}$/;

export const EMPTY_DEFINITION: Definition = {
  trigger: { type: "manual" },
  steps: [{ id: "s1", action: "approval", params: { reason: "Continue?" } }],
};

export function defaultParams(action: Action): Record<string, unknown> {
  if (action === "exec") return { node_id: "", command: "", read_only: true, timeout_s: 60 };
  if (action === "notify") return { channel_id: "", message: "" };
  return { reason: "" };
}

export function freeId(steps: Step[]): string {
  const used = new Set(steps.map((s) => s.id));
  for (let n = steps.length + 1; ; n++) if (!used.has(`s${n}`)) return `s${n}`;
}

/** The ids of the steps that have a problem (the graph editor outlines them). */
export function problemSteps(def: Definition): Set<string> {
  const bad = new Set<string>();
  const seen = new Set<string>();
  def.steps.forEach((s, i) => {
    let wrong = !ID_RE.test(s.id) || seen.has(s.id);
    seen.add(s.id);
    if (s.action === "exec") wrong ||= !s.params.node_id || !String(s.params.command ?? "").trim();
    if (s.action === "notify") wrong ||= !s.params.channel_id || !String(s.params.message ?? "").trim();
    if (s.when && !def.steps.slice(0, i).some((p) => p.id === s.when?.step)) wrong = true;
    if (wrong) bad.add(s.id);
  });
  return bad;
}

/** Problems that would make the API refuse the definition, in words. */
export function problems(def: Definition): string[] {
  const out: string[] = [];
  if (def.trigger.type === "interval" && !(Number(def.trigger.interval_s) >= 60)) out.push("The interval must be at least 60 seconds.");
  if (def.steps.length === 0) out.push("Add at least one step.");
  const seen = new Set<string>();
  def.steps.forEach((s, i) => {
    const at = `Step ${i + 1} (${s.id || "no id"})`;
    if (!ID_RE.test(s.id)) out.push(`${at}: the id must start with a letter and use a-z, 0-9, _ or - (32 characters at most).`);
    if (seen.has(s.id)) out.push(`${at}: the id is used twice.`);
    seen.add(s.id);
    if (s.action === "exec") {
      if (!s.params.node_id) out.push(`${at}: choose a node.`);
      if (!String(s.params.command ?? "").trim()) out.push(`${at}: enter the command.`);
    }
    if (s.action === "notify") {
      if (!s.params.channel_id) out.push(`${at}: choose a notification channel.`);
      if (!String(s.params.message ?? "").trim()) out.push(`${at}: enter the message.`);
    }
    if (s.when && !def.steps.slice(0, i).some((p) => p.id === s.when?.step)) out.push(`${at}: the condition must refer to an earlier step.`);
  });
  return out;
}

/** The definition as the API wants it: only the fields each action knows, no empty extras. */
export function normalize(def: Definition): Definition {
  const trigger: Trigger = { type: def.trigger.type };
  if (def.trigger.type === "interval") trigger.interval_s = Number(def.trigger.interval_s);
  if (def.trigger.type === "alert" && def.trigger.check_id) trigger.check_id = def.trigger.check_id;
  return {
    trigger,
    steps: def.steps.map((s) => {
      const p = s.params;
      const params: Record<string, unknown> =
        s.action === "exec"
          ? { node_id: p.node_id, command: String(p.command ?? "").trim(), read_only: Boolean(p.read_only), timeout_s: Number(p.timeout_s) || 60 }
          : s.action === "notify"
            ? { channel_id: p.channel_id, message: String(p.message ?? "").trim() }
            : { reason: String(p.reason ?? "") };
      const step: Step = { id: s.id, action: s.action, params };
      if (s.ui && Number.isFinite(s.ui.x) && Number.isFinite(s.ui.y)) step.ui = { x: Math.round(s.ui.x), y: Math.round(s.ui.y) };
      if (s.when) step.when = { step: s.when.step, outcome: s.when.outcome, ...(s.when.contains?.trim() ? { contains: s.when.contains.trim() } : {}) };
      if (s.action !== "approval" && s.on_failure === "continue") step.on_failure = "continue"; // an approval cannot fail
      return step;
    }),
  };
}

/** Reads a definition from JSON text, leniently: whatever is not understood is left for the API to refuse. */
export function parseDefinition(text: string): Definition | null {
  try {
    const raw = JSON.parse(text) as Partial<Definition>;
    if (!raw || typeof raw !== "object" || !raw.trigger || !Array.isArray(raw.steps)) return null;
    return raw as Definition;
  } catch {
    return null;
  }
}

