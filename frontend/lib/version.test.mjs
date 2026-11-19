// Agent versus server version. Run with `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { isOlder } from "./version.ts";

test("an older agent is older, an equal or newer one is not", () => {
  assert.equal(isOlder("0.8.1", "0.8.2"), true);
  assert.equal(isOlder("0.7.9", "0.8.0"), true);
  assert.equal(isOlder("0.9.0", "1.0.0"), true);
  assert.equal(isOlder("0.8.2", "0.8.2"), false);
  assert.equal(isOlder("0.8.3", "0.8.2"), false);
  assert.equal(isOlder("0.10.0", "0.9.0"), false); // numbers, not text
});

test("an unknown version is never reported as old", () => {
  assert.equal(isOlder(null, "0.8.2"), false);
  assert.equal(isOlder("", "0.8.2"), false);
  assert.equal(isOlder("dev", "0.8.2"), false);
  assert.equal(isOlder("0.8.1", null), false);
});
