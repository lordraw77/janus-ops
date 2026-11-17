// Rules of the visual workflow editor. Run with `npm test` (Node's own test runner; no dependency).
import assert from "node:assert/strict";
import { test } from "node:test";

import { EMPTY_DEFINITION, defaultParams, freeId, normalize, parseDefinition, problems } from "./workflows.ts";

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
