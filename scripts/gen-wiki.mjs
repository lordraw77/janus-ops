#!/usr/bin/env node
// Generates the GitHub wiki pages from the in-app Guide (frontend/lib/guide.ts).
//   node scripts/gen-wiki.mjs [outDir]      (default: docs/wiki)
// Screenshots (res/guide) are copied to images/ so the wiki is self-contained.
// Publish: copy the output into a clone of <repo>.wiki.git, commit and push.
import { cpSync, mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const out = resolve(process.argv[2] ?? join(root, "docs/wiki"));
const RAW = process.env.WIKI_IMAGE_BASE ?? "images";
const REPO = "https://github.com/lordraw77/janus-ops";

const { GUIDE } = await import(pathToFileURL(join(root, "frontend/lib/guide.ts")).href);

const pageName = (s) => s.title.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-|-$/g, "");
const link = (s) => `[${s.title}](${pageName(s)})`;

function block(b) {
  switch (b.t) {
    case "p": return b.text;
    case "h": return `### ${b.text}`;
    case "list": return b.items.map((it, i) => `${b.ordered ? `${i + 1}.` : "-"} ${it}`).join("\n");
    case "tip": return `> ${b.kind === "warn" ? "⚠️ **Warning**" : "💡 **Tip**"} — ${b.text}`;
    case "shot": return `![${b.alt}](${RAW}/${b.src.split("/").pop()})\n\n<sub>${b.caption}</sub>`;
  }
}

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
cpSync(join(root, "res/guide"), join(out, "images"), { recursive: true });

GUIDE.forEach((s, i) => {
  const prev = GUIDE[i - 1], next = GUIDE[i + 1];
  const body = [
    `# ${s.title}`,
    `*${s.group}* — ${s.summary}`,
    s.needs ? `**Needs:** ${s.needs}` : "",
    ...s.blocks.map(block),
    "---",
    [prev ? `← ${link(prev)}` : "", "[Home](Home)", next ? `${link(next)} →` : ""].filter(Boolean).join(" · "),
  ].filter(Boolean).join("\n\n");
  writeFileSync(join(out, `${pageName(s)}.md`), body + "\n");
});

const groups = [];
for (const s of GUIDE) {
  const g = groups.find((x) => x.name === s.group);
  if (g) g.items.push(s); else groups.push({ name: s.group, items: [s] });
}
const toc = groups.map((g) => `**${g.name}**\n\n${g.items.map((s) => `- ${link(s)}`).join("\n")}`).join("\n\n");

writeFileSync(join(out, "Home.md"), [
  "# Janus-Ops wiki",
  "Janus-Ops is one portal for monitoring, containers, virtualization, SSH, automation and AI troubleshooting. This wiki is the user guide, with screenshots of every feature; the same text is available inside the app under **Help → Guide**.",
  "> The screenshots come from a demonstration installation with sample hosts, so names, addresses and values are not real.",
  toc,
  `Installation, configuration and releases: see the [README](${REPO}#readme) and the [changelog](${REPO}/blob/main/CHANGELOG.md).`,
].join("\n\n") + "\n");

writeFileSync(join(out, "_Sidebar.md"), [`**[Home](Home)**`, ...groups.map((g) => `**${g.name}**\n\n${g.items.map((s) => `- ${link(s)}`).join("\n")}`)].join("\n\n") + "\n");
writeFileSync(join(out, "_Footer.md"), `Generated from \`frontend/lib/guide.ts\` by \`scripts/gen-wiki.mjs\` — edit the guide, not these pages.\n`);

console.log(`${readdirSync(out).length} files in ${out}`);
