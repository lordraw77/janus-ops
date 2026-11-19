// Environment badge colours. Run with `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { LEGACY_COLORS, PRESET_COLORS, envHex, envStyle } from "./envColor.ts";

test("the old names are fixed colours, and primary is no longer the green of ok", () => {
  assert.equal(envHex("ok"), "#10b981");
  assert.equal(envHex("crit"), "#ef4444");
  assert.notEqual(envHex("primary"), envHex("ok"));
  assert.equal(new Set(Object.values(LEGACY_COLORS)).size, 5);
});

test("a custom colour is kept, in lower case", () => {
  assert.equal(envHex("#FF8800"), "#ff8800");
  assert.equal(envHex("#00aaff"), "#00aaff");
});

test("nothing or something unreadable is neutral", () => {
  for (const v of [null, undefined, "", "red", "#fff", "#ggg000", "url(x)"]) assert.equal(envHex(v), null, String(v));
  assert.equal(envStyle(null), undefined);
});

test("the style carries the colour, a translucent border and a faint fill", () => {
  assert.deepEqual(envStyle("#ff8800"), { color: "#ff8800", borderColor: "#ff880066", backgroundColor: "#ff88001a" });
  assert.deepEqual(envStyle("warn"), { color: "#f59e0b", borderColor: "#f59e0b66", backgroundColor: "#f59e0b1a" });
});

test("every swatch is a distinct valid colour", () => {
  const values = PRESET_COLORS.map((c) => c.value);
  assert.equal(new Set(values).size, values.length);
  for (const v of values) assert.equal(envHex(v), v);
});
