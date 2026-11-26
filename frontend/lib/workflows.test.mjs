// Rules of the visual workflow editor. Run with `npm test` (Node's own test runner; no dependency).
import assert from "node:assert/strict";
import { test } from "node:test";

import { ACTION_GROUPS, ACTION_LABEL, EMPTY_DEFINITION, changesThings, defaultParams, freeId, normalize, parseDefinition, problems, waitsForPerson } from "./workflows.ts";

const NODE = "00000000-0000-0000-0000-00000000000a";
const CHANNEL = "00000000-0000-0000-0000-00000000000c";

const exec = (id, extra = {}) => ({
  id,
  action: "exec",
  params: { node_id: NODE, command: " uptime ", read_only: true, timeout_s: 30, ...extra },
});

test("a fresh definition is valid", () => {
  assert.deepEqual(problems(EMPTY_DEFINITION), []);
});

test("problems are said in words, with the step they belong to", () => {
  const def = {
    trigger: { type: "interval", interval_s: 10 },
    steps: [
      { id: "Bad Id", action: "exec", params: defaultParams("exec") },
      { id: "n1", action: "notify", params: defaultParams("notify") },
    ],
  };
  const found = problems(def);
  assert.ok(found.some((m) => m.includes("at least 60 seconds")));
  assert.ok(found.some((m) => m.startsWith("Step 1") && m.includes("id must start with a letter")));
  assert.ok(found.some((m) => m.startsWith("Step 1") && m.includes("choose a node")));
  assert.ok(found.some((m) => m.startsWith("Step 1") && m.includes("enter the command")));
  assert.ok(found.some((m) => m.startsWith("Step 2") && m.includes("choose a notification channel")));
  assert.ok(found.some((m) => m.startsWith("Step 2") && m.includes("enter the message")));
});

test("an empty list of steps and duplicate ids are refused", () => {
  assert.ok(problems({ trigger: { type: "manual" }, steps: [] }).some((m) => m.includes("at least one step")));
  const twice = { trigger: { type: "manual" }, steps: [exec("a"), exec("a")] };
  assert.ok(problems(twice).some((m) => m.includes("used twice")));
});

test("a condition may only look back", () => {
  const def = {
    trigger: { type: "manual" },
    steps: [{ ...exec("a"), when: { step: "b", outcome: "ok" } }, exec("b")],
  };
  assert.ok(problems(def).some((m) => m.includes("earlier step")));
  def.steps[0].when = undefined;
  def.steps[1].when = { step: "a", outcome: "failed" };
  assert.deepEqual(problems(def), []);
});

test("normalize keeps only what each action knows and trims text", () => {
  const out = normalize({
    trigger: { type: "manual", interval_s: 99, check_id: "x" },
    steps: [
      { ...exec("look"), params: { ...exec("look").params, stray: "dropped" }, on_failure: "continue" },
      {
        id: "alarm",
        action: "notify",
        params: { channel_id: CHANNEL, message: " it failed ", read_only: true },
        when: { step: "look", outcome: "failed", contains: "  " },
      },
      { id: "gate", action: "approval", params: { reason: "ok?", command: "x" }, on_failure: "continue" },
    ],
  });
  assert.deepEqual(out.trigger, { type: "manual" }); // interval and check only belong to their triggers
  assert.deepEqual(out.steps[0], {
    id: "look",
    action: "exec",
    params: { node_id: NODE, command: "uptime", read_only: true, timeout_s: 30 },
    on_failure: "continue",
  });
  assert.deepEqual(out.steps[1], {
    id: "alarm",
    action: "notify",
    params: { channel_id: CHANNEL, message: "it failed" },
    when: { step: "look", outcome: "failed" }, // an empty "contains" is left out
  });
  assert.deepEqual(out.steps[2], { id: "gate", action: "approval", params: { reason: "ok?" } }); // an approval cannot "carry on"
});

test("normalize writes the trigger options of the chosen trigger only", () => {
  const steps = [exec("a")];
  assert.deepEqual(normalize({ trigger: { type: "interval", interval_s: 120 }, steps }).trigger, { type: "interval", interval_s: 120 });
  assert.deepEqual(normalize({ trigger: { type: "alert", check_id: "c1" }, steps }).trigger, { type: "alert", check_id: "c1" });
  assert.deepEqual(normalize({ trigger: { type: "alert" }, steps }).trigger, { type: "alert" });
  assert.deepEqual(normalize({ trigger: { type: "webhook" }, steps }).trigger, { type: "webhook" });
});

test("what the editor writes is what it reads back", () => {
  const def = {
    trigger: { type: "interval", interval_s: 300 },
    steps: [
      { ...exec("look"), on_failure: "continue" },
      { id: "alarm", action: "notify", params: { channel_id: CHANNEL, message: "down" }, when: { step: "look", outcome: "failed" } },
    ],
  };
  const text = JSON.stringify(normalize(def));
  assert.deepEqual(normalize(parseDefinition(text)), normalize(def));
});

test("parseDefinition is lenient about contents and strict about shape", () => {
  assert.equal(parseDefinition("not json"), null);
  assert.equal(parseDefinition("[]"), null);
  assert.equal(parseDefinition('{"trigger": {"type": "manual"}}'), null);
  assert.ok(parseDefinition('{"trigger": {"type": "manual"}, "steps": []}'));
});

test("new step ids never collide", () => {
  assert.equal(freeId([]), "s1");
  assert.equal(freeId([{ id: "s1" }, { id: "s2" }]), "s3");
  assert.equal(freeId([{ id: "s2" }]), "s3"); // one step, "s2" taken: the next free one
  assert.equal(freeId([{ id: "a" }, { id: "s2" }, { id: "s3" }]), "s4");
});

// ---------------------------------------------------------------- management actions

const step = (action, params, extra = {}) => ({ id: "a", action, params: { ...defaultParams(action), ...params }, ...extra });
const def = (...steps) => ({ trigger: { type: "manual" }, steps });

test("every kind of step is in the add bar once, with a label", () => {
  const listed = ACTION_GROUPS.flatMap((g) => g.actions);
  assert.equal(new Set(listed).size, listed.length);
  assert.deepEqual(new Set(listed), new Set(Object.keys(ACTION_LABEL)));
});

test("only the steps that change something wait for a person, unless marked unattended", () => {
  assert.equal(changesThings(step("service", { op: "restart" })), true);
  assert.equal(changesThings(step("service", { op: "status" })), false);
  assert.equal(changesThings(step("process", { op: "kill" })), true);
  assert.equal(changesThings(step("container", { op: "stop" })), true);
  assert.equal(changesThings(step("proxmox", { op: "snapshot" })), true);
  assert.equal(changesThings(step("proxmox", { op: "status" })), false);
  assert.equal(changesThings(step("http", {})), false);
  assert.equal(changesThings(step("exec", { read_only: true })), false);
  assert.equal(waitsForPerson(step("service", { op: "restart" })), true);
  assert.equal(waitsForPerson(step("service", { op: "restart" }, { unattended: true })), false);
  assert.equal(waitsForPerson(step("approval", {}, { unattended: true })), true);
});

test("the fields of each management step are checked in words", () => {
  const said = (s) => problems(def(s)).join(" | ");
  assert.match(said(step("service", { node_id: NODE, name: "" })), /enter the service or unit name/);
  assert.match(said(step("service", { node_id: NODE, name: "ng inx;" })), /characters that are not allowed/);
  assert.match(said(step("container", { name: "web" })), /choose a node/);
  assert.match(said(step("process", { node_id: NODE, name: "java -9" })), /not allowed/);
  assert.match(said(step("http", { node_id: NODE, path: "healthz" })), /must start with \//);
  assert.match(said(step("port", { node_id: NODE, port: 70000 })), /between 1 and 65535/);
  assert.match(said(step("check", {})), /choose a check/);
  assert.match(said(step("proxmox", { cluster: NODE, pve_node: "", vmid: 50 })), /Proxmox node name.*\|.*100 or more/);
  assert.match(said(step("proxmox", { cluster: NODE, pve_node: "pve1", vmid: 100, op: "snapshot", snapshot: "" })), /name the snapshot/);
  assert.equal(problems(def(step("service", { node_id: NODE, name: "nginx" }))).length, 0);
});

test("a message may quote only an earlier step", () => {
  const notify = { id: "n", action: "notify", params: { channel_id: CHANNEL, message: "restarted: {{svc.output}}" } };
  const svc = { id: "svc", action: "service", params: { ...defaultParams("service"), node_id: NODE, name: "nginx" } };
  assert.deepEqual(problems(def(svc, notify)), []);
  assert.match(problems(def(notify, svc)).join(), /not an earlier step/);
});

test("normalize writes the right fields and types for each management step", () => {
  const out = normalize(
    def(
      step("service", { node_id: NODE, name: " nginx ", timeout_s: "45", junk: 1 }, { id: "s1" }),
      step("http", { node_id: NODE, port: "8080", contains: "  ", expect_status: "204" }, { id: "s2" }),
      step("proxmox", { cluster: NODE, pve_node: "pve1", vmid: "101", op: "reboot", snapshot: "left over" }, { id: "s3" }),
      step("wait", { seconds: "30" }, { id: "s4" }),
    ),
  ).steps;
  assert.deepEqual(out[0].params, { node_id: NODE, name: "nginx", op: "restart", expect: "running", timeout_s: 45 });
  assert.equal(out[1].params.port, 8080);
  assert.equal(out[1].params.expect_status, 204);
  assert.ok(!("contains" in out[1].params));
  assert.ok(!("snapshot" in out[2].params));
  assert.equal(out[2].params.vmid, 101);
  assert.deepEqual(out[3].params, { seconds: 30 });
});

test("retries and unattended are written only when they mean something", () => {
  const out = normalize(
    def(
      step("service", { node_id: NODE, name: "n" }, { id: "s1", retries: 3, retry_delay_s: 5, unattended: true }),
      step("service", { node_id: NODE, name: "n", op: "status" }, { id: "s2", retries: 0, retry_delay_s: 5, unattended: true }),
      step("approval", {}, { id: "s3", retries: 2, unattended: true }),
      step("wait", {}, { id: "s4", retries: 9, retry_delay_s: 999 }),
    ),
  ).steps;
  assert.deepEqual([out[0].retries, out[0].retry_delay_s, out[0].unattended], [3, 5, true]);
  assert.deepEqual([out[1].retries, out[1].unattended], [undefined, undefined]); // a read does not need the flag
  assert.deepEqual([out[2].retries, out[2].unattended], [undefined, undefined]);
  assert.deepEqual([out[3].retries, out[3].retry_delay_s], [5, 300]); // clamped to what the API accepts
});
