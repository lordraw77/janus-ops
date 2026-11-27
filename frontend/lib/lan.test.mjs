// LAN watch helpers. Run with `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { bars, displayName, matches, span } from "./lan.ts";

test("durations are shown with two units at most", () => {
  assert.equal(span(30_000), "30 s");
  assert.equal(span(10 * 60_000), "10 min");
  assert.equal(span((2 * 60 + 10) * 60_000), "2 h 10 min");
  assert.equal(span(3 * 3600_000), "3 h");
  assert.equal(span((3 * 24 + 4) * 3600_000), "3 d 4 h");
});

test("segments become percentages of the period, clipped to it", () => {
  const segs = [
    { start: "2026-11-27T00:00:00Z", end: "2026-11-27T01:00:00Z", online: true },
    { start: "2026-11-27T01:00:00Z", end: "2026-11-27T04:00:00Z", online: false },
  ];
  const out = bars(segs, "2026-11-27T00:00:00Z", "2026-11-27T04:00:00Z");
  assert.deepEqual(out.map((b) => [b.left, b.width, b.online]), [
    [0, 25, true],
    [25, 75, false],
  ]);
  assert.deepEqual(bars(segs, "2026-11-27T00:00:00Z", "2026-11-27T00:00:00Z"), []);
});

test("the shown name is the label, then the DNS name", () => {
  assert.equal(displayName({ label: "Printer", hostname: "hp.lan" }), "Printer");
  assert.equal(displayName({ label: null, hostname: "hp.lan" }), "hp.lan");
  assert.equal(displayName({ label: "", hostname: null }), null);
});

test("search looks at address, MAC and names", () => {
  const h = { ip: "192.168.0.10", mac: "aa:bb:cc:00:00:01", hostname: "nas.lan", label: null, subnet: "office", node_name: null };
  assert.ok(matches(h, "0.10"));
  assert.ok(matches(h, "AA:BB"));
  assert.ok(matches(h, "nas"));
  assert.ok(!matches(h, "printer"));
  assert.ok(matches(h, "  "));
});
