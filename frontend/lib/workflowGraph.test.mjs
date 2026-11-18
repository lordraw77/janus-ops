// What a gesture on the workflow canvas does to the definition. Run with `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  LANE_X, ROW_H, TRIGGER_ID, addStep, autoPosition, connect, describeStep, dropStep, freezeLayout,
  moveStep, orderByPosition, positionOf, removeBranch, removeStep, renameStep, setOutcome, setWhen, toGraph,
} from "./workflowGraph.ts";
import { normalize, problemSteps } from "./workflows.ts";

const NODE = "00000000-0000-0000-0000-00000000000a";
const exec = (id, extra = {}) => ({ id, action: "exec", params: { node_id: NODE, command: "uptime", read_only: true, timeout_s: 60 }, ...extra });
const flow = (...steps) => ({ trigger: { type: "manual" }, steps });
const ids = (def) => def.steps.map((s) => s.id);

test("a definition becomes a trigger, one node per step, the order and the branches", () => {
  const def = flow(exec("a"), exec("b", { when: { step: "a", outcome: "failed" } }), exec("c"));
  const { nodes, edges } = toGraph(def);
  assert.deepEqual(nodes.map((n) => n.id), [TRIGGER_ID, "a", "b", "c"]);
  assert.deepEqual(edges.filter((e) => e.type === "seq").map((e) => `${e.source}>${e.target}`), [`${TRIGGER_ID}>a`, "a>b", "b>c"]);
  const branches = edges.filter((e) => e.type === "branch");
  assert.equal(branches.length, 1);
  assert.deepEqual([branches[0].source, branches[0].target, branches[0].data.outcome], ["a", "b", "failed"]);
  assert.equal(branches[0].sourceHandle, "branch");
  assert.equal(branches[0].targetHandle, "cond");
});

test("a branch to a step that does not exist is not drawn", () => {
  const { edges } = toGraph(flow(exec("a", { when: { step: "ghost", outcome: "ok" } })));
  assert.equal(edges.filter((e) => e.type === "branch").length, 0);
});

test("steps are laid out as a column and branches get their own lane, unless the editor said otherwise", () => {
  const def = flow(exec("a"), exec("b", { when: { step: "a", outcome: "ok" } }));
  assert.deepEqual(autoPosition(def, 0), { x: 0, y: ROW_H });
  assert.deepEqual(autoPosition(def, 1), { x: LANE_X, y: 2 * ROW_H });
  def.steps[0].ui = { x: 5, y: 9 };
  assert.deepEqual(positionOf(def, 0), { x: 5, y: 9 });
});

test("status and problems reach the nodes", () => {
  const def = flow(exec("a"), { id: "b", action: "exec", params: { node_id: "", command: "" } });
  const { nodes } = toGraph(def, { a: "done" }, problemSteps(def));
  assert.equal(nodes.find((n) => n.id === "a").data.status, "done");
  assert.equal(nodes.find((n) => n.id === "a").data.issue, false);
  assert.equal(nodes.find((n) => n.id === "b").data.issue, true);
});

test("freezing the layout keeps every step where it is drawn", () => {
  const def = flow(exec("a"), exec("b", { when: { step: "a", outcome: "ok" } }));
  const frozen = freezeLayout(def);
  assert.deepEqual(frozen.steps.map((s) => s.ui), [{ x: 0, y: ROW_H }, { x: LANE_X, y: 2 * ROW_H }]);
});

test("dragging a step below another changes the order; a small move does not", () => {
  const def = flow(exec("a"), exec("b"), exec("c"));
  assert.deepEqual(ids(dropStep(def, "a", { x: 0, y: 3.4 * ROW_H })), ["b", "c", "a"]);
  assert.deepEqual(ids(dropStep(def, "c", { x: 0, y: 0 })), ["c", "a", "b"]);
  assert.deepEqual(ids(dropStep(def, "b", { x: 40, y: 2 * ROW_H + 10 })), ["a", "b", "c"]);
  assert.equal(orderByPosition(freezeLayout(def)).steps.length, 3);
});

test("a drag keeps the other steps where they were drawn", () => {
  const out = dropStep(flow(exec("a"), exec("b"), exec("c")), "a", { x: 0, y: 3.4 * ROW_H });
  assert.deepEqual(out.steps.map((s) => s.ui.y), [2 * ROW_H, 3 * ROW_H, Math.round(3.4 * ROW_H)]);
});

test("connecting two steps makes a branch, replacing the one the target had", () => {
  const def = flow(exec("a"), exec("b"), exec("c"));
  const one = connect(def, "a", "c").def;
  assert.deepEqual(one.steps[2].when, { step: "a", outcome: "failed" });
  const two = connect(one, "b", "c", "ok").def;
  assert.deepEqual(two.steps[2].when, { step: "b", outcome: "ok" });
  assert.deepEqual(ids(two), ["a", "b", "c"]);
});

test("a branch always looks back: connecting forward-to-back moves the target below the source", () => {
  const def = flow(exec("a"), exec("b"), exec("c"));
  const { def: out, note } = connect(def, "c", "a", "failed");
  assert.deepEqual(ids(out), ["b", "c", "a"]);
  assert.deepEqual(out.steps[2].when, { step: "c", outcome: "failed" });
  assert.ok(note.includes("a now comes after c"));
  assert.deepEqual(out.steps.map((s) => s.ui.y), [ROW_H, 2 * ROW_H, 3 * ROW_H]);
  assert.deepEqual(problemSteps(out), new Set()); // nothing left looking forward
});

test("connecting a step to itself, or to something that is not a step, does nothing", () => {
  const def = flow(exec("a"), exec("b"));
  assert.equal(connect(def, "a", "a").def, def);
  assert.equal(connect(def, "a", TRIGGER_ID).def, def);
  assert.equal(connect(def, "zzz", "b").def, def);
});

test("the outcome of a branch can be changed and the branch removed", () => {
  const def = connect(flow(exec("a"), exec("b")), "a", "b").def;
  assert.equal(setOutcome(def, "b", "skipped").steps[1].when.outcome, "skipped");
  assert.equal(setOutcome(def, "a", "ok").steps[0].when, undefined); // a has no branch: nothing to change
  assert.equal(removeBranch(def, "b").steps[1].when, undefined);
});

test("removing a step takes the branches that looked at it", () => {
  const def = connect(connect(flow(exec("a"), exec("b"), exec("c")), "a", "b").def, "b", "c").def;
  const out = removeStep(def, "b");
  assert.deepEqual(ids(out), ["a", "c"]);
  assert.equal(out.steps[1].when, undefined);
  assert.deepEqual(out.steps[0].when, undefined);
});

test("a new step lands below the last one, with an id that is free", () => {
  const def = flow(exec("s1"), exec("s3"));
  const out = addStep(def, "notify");
  assert.equal(out.steps[2].id, "s4");
  assert.equal(out.steps[2].action, "notify");
  assert.deepEqual(out.steps[2].ui, { x: 0, y: 3 * ROW_H });
});

test("positions survive normalisation as whole numbers; a step without one stays without", () => {
  const def = flow({ ...exec("a"), ui: { x: 10.6, y: 20.2 } }, exec("b"));
  const out = normalize(def);
  assert.deepEqual(out.steps[0].ui, { x: 11, y: 20 });
  assert.equal("ui" in out.steps[1], false);
});

test("a step says in one line what it does", () => {
  assert.equal(describeStep(exec("a")), "uptime");
  assert.equal(describeStep({ id: "n", action: "notify", params: { message: "hi" } }), "hi");
  assert.equal(describeStep({ id: "g", action: "approval", params: { reason: "" } }), "waits for a person");
  assert.equal(describeStep({ id: "e", action: "exec", params: { command: "" } }), "no command yet");
});

test("moving a step only changes its position", () => {
  const def = flow(exec("a"), exec("b"));
  const out = moveStep(def, "a", { x: 3.7, y: 4.2 });
  assert.deepEqual(out.steps[0].ui, { x: 4, y: 4 });
  assert.equal(out.steps[1], def.steps[1]);
});

test("renaming a step updates the branches that look at it", () => {
  const def = connect(flow(exec("a"), exec("b")), "a", "b").def;
  const out = renameStep(def, "a", "check");
  assert.deepEqual(ids(out), ["check", "b"]);
  assert.equal(out.steps[1].when.step, "check");
});

test("a condition can be set and cleared directly", () => {
  const def = flow(exec("a"), exec("b"));
  const set = setWhen(def, "b", { step: "a", outcome: "ok", contains: "active" });
  assert.deepEqual(set.steps[1].when, { step: "a", outcome: "ok", contains: "active" });
  assert.equal(setWhen(set, "b", null).steps[1].when, undefined);
});
