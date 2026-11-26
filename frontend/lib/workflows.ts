/** Workflow definitions as the API stores them (FR-80), and the pure rules the visual editor needs:
 * validation in words, normalisation into what the API accepts, lenient JSON reading. No UI here, so
 * it can be tested on its own (lib/workflows.test.mjs). */

/** The workflow definition the API stores: a trigger and an ordered list of steps (FR-80). */
export type TriggerType = "manual" | "interval" | "alert" | "webhook";
export type Trigger = { type: TriggerType; interval_s?: number; check_id?: string };
export type Action = "exec" | "notify" | "approval" | "service" | "process" | "container" | "http" | "port" | "wait" | "check" | "proxmox";
export type Outcome = "ok" | "failed" | "skipped";
export type When = { step: string; outcome: Outcome; contains?: string };
export type Pos = { x: number; y: number };
export type Step = {
  id: string;
  action: Action;
  params: Record<string, unknown>;
  when?: When;
  on_failure?: "fail" | "continue";
  /** tried again this many times (0-5), this many seconds apart, when it fails */
  retries?: number;
  retry_delay_s?: number;
  /** runs without waiting for a person even though it changes something (audited) */
  unattended?: boolean;
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

/** The kinds of step, as the "add a step" bar groups them. */
export const ACTION_GROUPS: { title: string; actions: Action[] }[] = [
  { title: "Act", actions: ["service", "container", "process", "proxmox", "exec"] },
  { title: "Check", actions: ["http", "port", "check"] },
  { title: "Flow", actions: ["wait", "notify", "approval"] },
];

export const ACTION_LABEL: Record<Action, string> = {
  exec: "Command",
  service: "Service",
  process: "Process",
  container: "Container",
  proxmox: "Proxmox guest",
  http: "HTTP check",
  port: "Port check",
  check: "Check state",
  wait: "Wait",
  notify: "Notification",
  approval: "Approval",
};

/** One sentence per kind of step, shown as the hint of the add button. */
export const ACTION_HINT: Record<Action, string> = {
  exec: "Run a command through the agent (no shell).",
  service: "Start, stop, restart, reload or check a systemd unit or Windows service.",
  process: "Check that a process is running (or gone), or kill it by name.",
  container: "Start, stop, restart or check a Docker container.",
  proxmox: "Start, shut down, reboot, snapshot or check a Proxmox VM or container.",
  http: "Request a page of the node and check the status and the text.",
  port: "Check that a TCP port of the node is open (or closed).",
  check: "Read the state of a check, optionally waiting until it is OK.",
  wait: "Pause before the next step.",
  notify: "Send a message to a notification channel.",
  approval: "Stop until a person approves.",
};

const MUTATING: Partial<Record<Action, string[]>> = {
  service: ["start", "stop", "restart", "reload"],
  process: ["kill"],
  container: ["start", "stop", "restart"],
  proxmox: ["start", "shutdown", "stop", "reboot", "snapshot"],
};

/** Whether the step changes something on a host (the API makes such a step wait for a person). */
export function changesThings(step: Step): boolean {
  if (step.action === "exec") return !step.params.read_only;
  return (MUTATING[step.action] ?? []).includes(String(step.params.op ?? ""));
}

/** Whether a run stops at this step until a person decides. */
export function waitsForPerson(step: Step): boolean {
  return step.action === "approval" || (changesThings(step) && !step.unattended);
}

export function defaultParams(action: Action): Record<string, unknown> {
  switch (action) {
    case "exec":
      return { node_id: "", command: "", read_only: true, timeout_s: 60 };
    case "notify":
      return { channel_id: "", message: "" };
    case "service":
      return { node_id: "", name: "", op: "restart", expect: "running", timeout_s: 60 };
    case "process":
      return { node_id: "", name: "", op: "check", expect: "running", signal: "TERM", timeout_s: 30 };
    case "container":
      return { node_id: "", name: "", op: "restart", expect: "running" };
    case "http":
      return { node_id: "", scheme: "http", port: 80, path: "/", method: "GET", expect_status: 200, contains: "", verify_tls: false, timeout_s: 10 };
    case "port":
      return { node_id: "", port: 22, expect: "open", timeout_s: 5 };
    case "wait":
      return { seconds: 10 };
    case "check":
      return { check_id: "", state: "ok", wait_s: 60 };
    case "proxmox":
      return { cluster: "", pve_node: "", kind: "qemu", vmid: 100, op: "status", expect: "running", snapshot: "", wait_s: 120 };
    default:
      return { reason: "" };
  }
}

/** The node (or Proxmox host) a step acts on, when it has one. */
const NODE_KEY: Partial<Record<Action, string>> = { exec: "node_id", service: "node_id", process: "node_id", container: "node_id", http: "node_id", port: "node_id", proxmox: "cluster" };

export function freeId(steps: Step[]): string {
  const used = new Set(steps.map((s) => s.id));
  for (let n = steps.length + 1; ; n++) if (!used.has(`s${n}`)) return `s${n}`;
}

const NAME_RE = {
  service: /^[A-Za-z0-9@._:-]{1,128}$/,
  process: /^[A-Za-z0-9._+-]{1,64}$/,
  container: /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/,
};
const TEMPLATE_RE = /\{\{\s*([a-z][a-z0-9_-]{0,31})\.(output|status)\s*\}\}/g;

/** What is missing or wrong in one step's own fields, in words. `earlier` are the steps before it. */
export function stepIssues(s: Step, earlier: Step[]): string[] {
  const p = s.params;
  const out: string[] = [];
  const key = NODE_KEY[s.action];
  if (key && !p[key]) out.push(s.action === "proxmox" ? "choose a Proxmox host of the cluster." : "choose a node.");
  if (s.action === "exec" && !String(p.command ?? "").trim()) out.push("enter the command.");
  if (s.action === "notify") {
    if (!p.channel_id) out.push("choose a notification channel.");
    if (!String(p.message ?? "").trim()) out.push("enter the message.");
    for (const m of String(p.message ?? "").matchAll(TEMPLATE_RE)) {
      if (!earlier.some((e) => e.id === m[1])) out.push(`the message quotes {{${m[1]}…}}, which is not an earlier step.`);
    }
  }
  if (s.action === "service" || s.action === "process" || s.action === "container") {
    const name = String(p.name ?? "").trim();
    const what = s.action === "service" ? "the service or unit name" : s.action === "process" ? "the process name" : "the container name";
    if (!name) out.push(`enter ${what}.`);
    else if (!NAME_RE[s.action].test(name)) out.push(`${what} has characters that are not allowed.`);
  }
  if (s.action === "http" && !/^\/[^\s]*$/.test(String(p.path ?? ""))) out.push("the path must start with / and have no spaces.");
  if ((s.action === "http" || s.action === "port") && !(Number(p.port) >= 1 && Number(p.port) <= 65535)) out.push("the port must be between 1 and 65535.");
  if (s.action === "check" && !p.check_id) out.push("choose a check.");
  if (s.action === "proxmox") {
    if (!/^[A-Za-z0-9][A-Za-z0-9-]{0,62}$/.test(String(p.pve_node ?? ""))) out.push("enter the Proxmox node name (as in the Proxmox page).");
    if (!(Number(p.vmid) >= 100)) out.push("the guest id must be 100 or more.");
    if (p.op === "snapshot" && !/^[A-Za-z][A-Za-z0-9_-]{1,39}$/.test(String(p.snapshot ?? ""))) out.push("name the snapshot (a letter first, then letters, digits, _ or -).");
  }
  return out;
}

/** The ids of the steps that have a problem (the graph editor outlines them). */
export function problemSteps(def: Definition): Set<string> {
  const bad = new Set<string>();
  const seen = new Set<string>();
  def.steps.forEach((s, i) => {
    let wrong = !ID_RE.test(s.id) || seen.has(s.id);
    seen.add(s.id);
    wrong ||= stepIssues(s, def.steps.slice(0, i)).length > 0;
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
    for (const issue of stepIssues(s, def.steps.slice(0, i))) out.push(`${at}: ${issue}`);
    if (s.when && !def.steps.slice(0, i).some((p) => p.id === s.when?.step)) out.push(`${at}: the condition must refer to an earlier step.`);
  });
  return out;
}

const num = (v: unknown, fallback: number) => (Number.isFinite(Number(v)) && v !== "" ? Number(v) : fallback);
const str = (v: unknown) => String(v ?? "").trim();

/** Only the fields each action knows, with the right types. */
function normalizeParams(action: Action, p: Record<string, unknown>): Record<string, unknown> {
  switch (action) {
    case "exec":
      return { node_id: p.node_id, command: str(p.command), read_only: Boolean(p.read_only), timeout_s: num(p.timeout_s, 60) || 60 };
    case "notify":
      return { channel_id: p.channel_id, message: str(p.message) };
    case "service":
      return { node_id: p.node_id, name: str(p.name), op: p.op, expect: p.expect ?? "running", timeout_s: num(p.timeout_s, 60) };
    case "process":
      return { node_id: p.node_id, name: str(p.name), op: p.op, expect: p.expect ?? "running", signal: p.signal ?? "TERM", timeout_s: num(p.timeout_s, 30) };
    case "container":
      return { node_id: p.node_id, name: str(p.name), op: p.op, expect: p.expect ?? "running" };
    case "http": {
      const out: Record<string, unknown> = { node_id: p.node_id, scheme: p.scheme, port: num(p.port, 80), path: str(p.path) || "/", method: p.method, expect_status: num(p.expect_status, 200), verify_tls: Boolean(p.verify_tls), timeout_s: num(p.timeout_s, 10) };
      if (str(p.contains)) out.contains = str(p.contains);
      return out;
    }
    case "port":
      return { node_id: p.node_id, port: num(p.port, 22), expect: p.expect ?? "open", timeout_s: num(p.timeout_s, 5) };
    case "wait":
      return { seconds: num(p.seconds, 10) };
    case "check":
      return { check_id: p.check_id, state: p.state ?? "ok", wait_s: num(p.wait_s, 0) };
    case "proxmox": {
      const out: Record<string, unknown> = { cluster: p.cluster, pve_node: str(p.pve_node), kind: p.kind, vmid: num(p.vmid, 100), op: p.op, expect: p.expect ?? "running", wait_s: num(p.wait_s, 120) };
      if (p.op === "snapshot") out.snapshot = str(p.snapshot);
      return out;
    }
    default:
      return { reason: String(p.reason ?? "") };
  }
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
      const params = normalizeParams(s.action, p);
      const step: Step = { id: s.id, action: s.action, params };
      if (s.ui && Number.isFinite(s.ui.x) && Number.isFinite(s.ui.y)) step.ui = { x: Math.round(s.ui.x), y: Math.round(s.ui.y) };
      if (s.when) step.when = { step: s.when.step, outcome: s.when.outcome, ...(s.when.contains?.trim() ? { contains: s.when.contains.trim() } : {}) };
      if (s.action !== "approval" && s.on_failure === "continue") step.on_failure = "continue"; // an approval cannot fail
      if (s.action !== "approval" && Number(s.retries) > 0) {
        step.retries = Math.min(5, Math.floor(Number(s.retries)));
        step.retry_delay_s = Math.min(300, Math.max(1, Math.floor(Number(s.retry_delay_s) || 10)));
      }
      if (s.unattended && changesThings(s)) step.unattended = true;
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

