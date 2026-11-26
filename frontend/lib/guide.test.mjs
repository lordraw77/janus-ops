import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(new URL("./guide.ts", import.meta.url), "utf8");
const dir = new URL("../../res/guide/", import.meta.url);

test("every screenshot the guide shows exists", () => {
  const names = [...source.matchAll(/shot\("([a-z0-9-]+)"/g)].map((m) => m[1]);
  assert.ok(names.length > 20);
  for (const name of names) assert.ok(existsSync(new URL(`${name}.png`, dir)), `missing res/guide/${name}.png`);
});

test("no screenshot is left unused", () => {
  const used = new Set([...source.matchAll(/shot\("([a-z0-9-]+)"/g)].map((m) => `${m[1]}.png`));
  for (const file of readdirSync(dir)) assert.ok(used.has(file), `${file} is not in the guide`);
});

test("chapter ids are unique and the menu points at the guide", () => {
  const ids = [...source.matchAll(/^\s{4}id: "([a-z-]+)"/gm)].map((m) => m[1]);
  assert.equal(new Set(ids).size, ids.length);
  const nav = readFileSync(new URL("../components/Sidebar.tsx", import.meta.url), "utf8");
  assert.match(nav, /label: "Guide"[^\n]*href: "\/guide\/"/);
});
