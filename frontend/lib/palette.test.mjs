// What the command palette offers and in which order. Run with `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { move, rank, score } from "./palette.ts";

const page = (label, href, extra = {}) => ({ id: href, label, href, group: "Go to", ...extra });
const node = (name, kind, href) => ({ id: href, label: `${kind} ${name}`, hint: name, href, group: "Nodes", keywords: name });
const ENTRIES = [
  page("Dashboard", "/"),
  page("Problems", "/problems/"),
  page("SSH Bastion", "/terminal/", { keywords: "terminal shell" }),
  page("Janus AI Copilot", "/ai/", { keywords: "ask diagnose" }),
  node("docker2", "Open SSH on", "/terminal/?node=2"),
  node("docker2", "Open", "/nodes/?id=2"),
  node("pve01", "Open", "/nodes/?id=1"),
];
const labels = (q) => rank(ENTRIES, q).map((e) => e.href);

test("with nothing typed the pages are listed, in their order", () => {
  assert.deepEqual(labels(""), ["/", "/problems/", "/terminal/", "/ai/"]);
  assert.deepEqual(labels("   "), labels(""));
});

test("every word typed has to start a word of the entry", () => {
  assert.deepEqual(labels("pve"), ["/nodes/?id=1"]);
  assert.deepEqual(labels("ssh docker"), ["/terminal/?node=2"]);
  assert.deepEqual(labels("zzz"), []);
  assert.deepEqual(labels("ocker"), []); // the middle of a word is not a match
});

test("a match in the label beats one only in the hint or the keywords", () => {
  assert.equal(score(ENTRIES[2], "shell"), 1);
  assert.equal(score(ENTRIES[2], "ssh"), 3);
  assert.equal(labels("ssh")[0], "/terminal/");
});

test("actions are found by what they do and by the node they act on", () => {
  const run = { id: "c1", label: "Run check Systemd units on e2e-debian", hint: "Check now", group: "Actions", act: { kind: "run-check", checkId: "c1" }, keywords: "execute verify" };
  const ask = { id: "p1", label: "Ask the AI about e2e-debian · Systemd units", hint: "Copilot", group: "Actions", href: "/ai/?problem=p1", keywords: "diagnose why" };
  const all = [...ENTRIES, run, ask];
  assert.deepEqual(rank(all, "run check").map((e) => e.id), ["c1"]);
  assert.deepEqual(rank(all, "ask debian").map((e) => e.id), ["p1"]);
  assert.deepEqual(rank(all, "why").map((e) => e.id), ["p1"]);
  assert.deepEqual(rank(all, "").map((e) => e.group), ["Go to", "Go to", "Go to", "Go to"]); // nothing typed: pages only
});

test("it is not case sensitive and ignores punctuation", () => {
  assert.deepEqual(labels("DOCKER2"), ["/terminal/?node=2", "/nodes/?id=2"]);
  assert.deepEqual(labels("ask: diagnose!"), ["/ai/"]);
});

test("at most twelve are returned", () => {
  const many = Array.from({ length: 40 }, (_, i) => node(`web${i}`, "Open", `/nodes/?id=${i}`));
  assert.equal(rank(many, "web").length, 12);
});

test("the selection wraps around", () => {
  assert.equal(move(0, -1, 4), 3);
  assert.equal(move(3, 1, 4), 0);
  assert.equal(move(0, 1, 0), 0);
});
