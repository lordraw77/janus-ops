import assert from "node:assert/strict";
import test from "node:test";

import { nextSort, sortRows } from "./sort.ts";

const rows = [
  { n: "node10", v: 5 },
  { n: "node2", v: null },
  { n: "Node1", v: 20 },
  { n: "", v: 1 },
];
const getters = { n: (r) => r.n, v: (r) => r.v };

test("names sort naturally and without regard to case, empty values go last", () => {
  assert.deepEqual(sortRows(rows, { col: "n", dir: "asc" }, getters).map((r) => r.n), ["Node1", "node2", "node10", ""]);
  assert.deepEqual(sortRows(rows, { col: "n", dir: "desc" }, getters).map((r) => r.n), ["node10", "node2", "Node1", ""]);
});

test("numbers sort as numbers and null stays last in both directions", () => {
  assert.deepEqual(sortRows(rows, { col: "v", dir: "asc" }, getters).map((r) => r.v), [1, 5, 20, null]);
  assert.deepEqual(sortRows(rows, { col: "v", dir: "desc" }, getters).map((r) => r.v), [20, 5, 1, null]);
});

test("no sort, or an unknown column, keeps the original order (and does not touch the input)", () => {
  assert.deepEqual(sortRows(rows, null, getters), rows);
  assert.deepEqual(sortRows(rows, { col: "zz", dir: "asc" }, getters), rows);
  assert.equal(rows[0].n, "node10");
});

test("the header cycle is ascending, descending, off", () => {
  const a = nextSort(null, "n");
  assert.deepEqual(a, { col: "n", dir: "asc" });
  const b = nextSort(a, "n");
  assert.deepEqual(b, { col: "n", dir: "desc" });
  assert.equal(nextSort(b, "n"), null);
  assert.deepEqual(nextSort(b, "v"), { col: "v", dir: "asc" });
});
