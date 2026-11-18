// Undo and redo. Run with `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { BURST_MS, LIMIT, canRedo, canUndo, fresh, push, redo, undo } from "./history.ts";

test("a fresh history has nothing to undo or redo", () => {
  const h = fresh("a");
  assert.equal(canUndo(h), false);
  assert.equal(canRedo(h), false);
  assert.equal(undo(h), h);
  assert.equal(redo(h), h);
});

test("changes far apart in time are separate steps; undo and redo walk through them", () => {
  let h = fresh("a");
  h = push(h, "b", 0);
  h = push(h, "c", 10_000);
  assert.equal(h.present, "c");
  h = undo(h);
  assert.equal(h.present, "b");
  h = undo(h);
  assert.equal(h.present, "a");
  assert.equal(canUndo(h), false);
  h = redo(redo(h));
  assert.equal(h.present, "c");
  assert.equal(canRedo(h), false);
});

test("the first change is always a step of its own", () => {
  const h = push(fresh("a"), "b", 5);
  assert.deepEqual(h.past, ["a"]);
});

test("changes that follow each other quickly are one step (typing in a field)", () => {
  let h = push(fresh("a"), "b", 1000);
  h = push(h, "bc", 1000 + BURST_MS - 1);
  h = push(h, "bcd", 1000 + BURST_MS + 100);
  assert.deepEqual(h.past, ["a"]);
  assert.equal(h.present, "bcd");
  assert.equal(undo(h).present, "a");
});

test("a change after an undo drops what could have been redone", () => {
  let h = push(push(fresh("a"), "b", 0), "c", 10_000);
  h = undo(h);
  h = push(h, "x", 20_000);
  assert.equal(canRedo(h), false);
  assert.deepEqual(h.past, ["a", "b"]);
});

test("a change right after an undo is not merged into the step before it", () => {
  let h = push(push(fresh("a"), "b", 0), "c", 10_000);
  h = undo(h); // at is reset
  h = push(h, "x", 10_001);
  assert.deepEqual(h.past, ["a", "b"]);
  assert.equal(undo(h).present, "b");
});

test("the same value again is not a change", () => {
  const obj = { a: 1 };
  const h = push(fresh(obj), obj, 0);
  assert.equal(canUndo(h), false);
});

test("only the last hundred steps are kept", () => {
  let h = fresh(0);
  for (let i = 1; i <= LIMIT + 20; i++) h = push(h, i, i * 10_000);
  assert.equal(h.past.length, LIMIT);
  assert.equal(h.past[0], 20);
});
