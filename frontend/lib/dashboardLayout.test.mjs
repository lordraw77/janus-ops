// Rules of the Dashboard card layout. Run with `npm test` (Node's own test runner).
import assert from "node:assert/strict";
import { test } from "node:test";

import { DEFAULT_LAYOUT, directionLabel, metricOf, moveBefore, readLayout, sortNodes } from "./dashboardLayout.ts";

const node = (id, name, over = {}) => ({
  node_id: id, name, address: "10.0.0.1", environment: "lab", state: "up",
  latest: { cpu_pct: null, mem_pct: null, disk_pct: null, load1: null, ...over },
});
const env = () => 0;
const names = (list) => list.map((n) => n.name);
const nodes = [node("1", "pi", { cpu_pct: 5 }), node("2", "NAS", { cpu_pct: 80 }), node("3", "alpha", {}), node("4", "web-10", { cpu_pct: 30 }), node("5", "web-2", { cpu_pct: 30 })];

test("by name, A-Z and Z-A, ignoring case and reading numbers as numbers", () => {
  assert.deepEqual(names(sortNodes(nodes, { ...DEFAULT_LAYOUT, mode: "name", dir: "asc" }, env)), ["alpha", "NAS", "pi", "web-2", "web-10"]);
  assert.deepEqual(names(sortNodes(nodes, { ...DEFAULT_LAYOUT, mode: "name", dir: "desc" }, env)), ["web-10", "web-2", "pi", "NAS", "alpha"]);
});

test("by load, the busiest first or last, nodes without a reading always at the end", () => {
  const high = names(sortNodes(nodes, { ...DEFAULT_LAYOUT, mode: "load", dir: "desc", metric: "cpu" }, env));
  assert.deepEqual(high, ["NAS", "web-2", "web-10", "pi", "alpha"]);
  const low = names(sortNodes(nodes, { ...DEFAULT_LAYOUT, mode: "load", dir: "asc", metric: "cpu" }, env));
  assert.deepEqual(low, ["pi", "web-2", "web-10", "NAS", "alpha"]);
});

test("each metric reads its own number", () => {
  const n = node("1", "a", { cpu_pct: 1, mem_pct: 2, disk_pct: 3, load1: 4 });
  assert.deepEqual(["cpu", "ram", "disk", "load"].map((m) => metricOf(n, m)), [1, 2, 3, 4]);
  assert.equal(metricOf(node("1", "a", { cpu_pct: NaN }), "cpu"), null);
});

test("a custom order puts the dragged nodes first and new nodes after, in the tidy order", () => {
  const layout = { ...DEFAULT_LAYOUT, mode: "custom", order: ["4", "1"] };
  assert.deepEqual(names(sortNodes(nodes, layout, env)), ["web-10", "pi", "alpha", "NAS", "web-2"]);
  const gone = { ...layout, order: ["9", "4"] }; // a node that no longer exists is ignored
  assert.deepEqual(names(sortNodes(nodes, gone, env)).slice(0, 2), ["web-10", "alpha"]);
});

test("the tidy order puts hosts without an agent last and groups by environment", () => {
  const list = [node("1", "b", {}), { ...node("2", "a"), state: "unmonitored" }, { ...node("3", "z"), environment: "prod" }];
  const rank = (e) => (e === "prod" ? 0 : 1);
  assert.deepEqual(names(sortNodes(list, { ...DEFAULT_LAYOUT, mode: "custom" }, rank)), ["z", "b", "a"]);
});

test("dropping a card moves it before the target, or to the end", () => {
  assert.deepEqual(moveBefore(["a", "b", "c", "d"], "d", "b"), ["a", "d", "b", "c"]);
  assert.deepEqual(moveBefore(["a", "b", "c", "d"], "a", "d"), ["b", "c", "a", "d"]);
  assert.deepEqual(moveBefore(["a", "b", "c"], "a", null), ["b", "c", "a"]);
  assert.deepEqual(moveBefore(["a", "b"], "a", "a"), ["a", "b"]);
  assert.deepEqual(moveBefore(["a", "b"], "z", "b"), ["a", "z", "b"]);
});

test("the saved choice is read leniently", () => {
  assert.deepEqual(readLayout(null), DEFAULT_LAYOUT);
  assert.deepEqual(readLayout("not json"), DEFAULT_LAYOUT);
  assert.deepEqual(readLayout('{"mode":"load","dir":"desc","metric":"disk","compact":true,"order":["a",1,"b"]}'), {
    mode: "load", dir: "desc", metric: "disk", compact: true, order: ["a", "b"],
  });
  assert.equal(readLayout('{"mode":"weird","metric":"x"}').mode, "name");
  assert.equal(readLayout('{"mode":"weird","metric":"x"}').metric, "cpu");
});

test("the direction button says what it does", () => {
  assert.equal(directionLabel("name", "asc"), "A → Z");
  assert.equal(directionLabel("name", "desc"), "Z → A");
  assert.equal(directionLabel("load", "desc"), "Highest first");
  assert.equal(directionLabel("load", "asc"), "Lowest first");
});
