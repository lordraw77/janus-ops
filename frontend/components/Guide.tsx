"use client";

import { BookOpen, Info, Lightbulb, Search, TriangleAlert } from "lucide-react";
import { Fragment, type ReactNode, useEffect, useMemo, useState } from "react";

import { type Block, GUIDE, type GuideSection } from "@/lib/guide";

/** **bold** and `code` inside a line of the guide. */
function inline(text: string): ReactNode {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return (
        <strong key={i} className="font-semibold text-fg">
          {part.slice(2, -2)}
        </strong>
      );
    }
    if (part.startsWith("`") && part.endsWith("`")) {
      return (
        <code key={i} className="rounded bg-elevated px-1 py-0.5 font-mono text-[12px] text-telemetry">
          {part.slice(1, -1)}
        </code>
      );
    }
    return <Fragment key={i}>{part}</Fragment>;
  });
}

function BlockView({ block }: { block: Block }) {
  switch (block.t) {
    case "p":
      return <p className="text-sm leading-relaxed text-fg-secondary">{inline(block.text)}</p>;
    case "h":
      return <h3 className="pt-2 text-sm font-semibold text-fg">{block.text}</h3>;
    case "list": {
      const Tag = block.ordered ? "ol" : "ul";
      return (
        <Tag className={`space-y-1.5 pl-5 text-sm leading-relaxed text-fg-secondary ${block.ordered ? "list-decimal" : "list-disc"} marker:text-fg-muted`}>
          {block.items.map((item) => (
            <li key={item}>{inline(item)}</li>
          ))}
        </Tag>
      );
    }
    case "tip": {
      const warn = block.kind === "warn";
      const Icon = warn ? TriangleAlert : Lightbulb;
      return (
        <div className={`flex gap-2.5 rounded-md border px-3 py-2.5 text-sm leading-relaxed ${warn ? "border-warn/40 bg-warn/10 text-fg-secondary" : "border-primary/30 bg-primary/5 text-fg-secondary"}`}>
          <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${warn ? "text-warn" : "text-primary"}`} aria-hidden />
          <p>{inline(block.text)}</p>
        </div>
      );
    }
    case "shot":
      return (
        <figure className="space-y-2">
          <a href={block.src} target="_blank" rel="noreferrer" title="Open the full-size image">
            {/* eslint-disable-next-line @next/next/no-img-element -- static export, no optimizer */}
            <img
              src={block.src}
              alt={block.alt}
              loading="lazy"
              width={1440}
              height={900}
              className="aspect-[16/10] h-auto w-full rounded-lg border border-line shadow-lg shadow-black/30 transition-colors hover:border-line-strong"
            />
          </a>
          <figcaption className="text-xs text-fg-muted">{inline(block.caption)}</figcaption>
        </figure>
      );
  }
}

function plain(section: GuideSection): string {
  const parts = [section.title, section.summary];
  for (const b of section.blocks) {
    if (b.t === "list") parts.push(...b.items);
    else if (b.t === "shot") parts.push(b.caption);
    else parts.push(b.text);
  }
  return parts.join(" ").toLowerCase();
}

export function GuideConsole() {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState<string>(GUIDE[0].id);

  const index = useMemo(() => GUIDE.map((s) => ({ s, text: plain(s) })), []);
  const needle = query.trim().toLowerCase();
  const shown = useMemo(
    () => (needle ? index.filter((x) => x.text.includes(needle)).map((x) => x.s) : GUIDE),
    [index, needle],
  );

  // a link like /guide/#traps: the page renders after sign-in, so jump once it is there
  useEffect(() => {
    const id = window.location.hash.slice(1);
    if (id) document.getElementById(id)?.scrollIntoView();
  }, []);

  // highlight the chapter being read in the contents
  useEffect(() => {
    const els = shown.map((s) => document.getElementById(s.id)).filter((e): e is HTMLElement => e !== null);
    if (els.length === 0 || typeof IntersectionObserver === "undefined") return;
    const obs = new IntersectionObserver(
      (entries) => {
        const top = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
        if (top) setActive(top.target.id);
      },
      { rootMargin: "-10% 0px -80% 0px" },
    );
    els.forEach((el) => obs.observe(el));
    return () => obs.disconnect();
  }, [shown]);

  const groups = useMemo(() => {
    const out: { name: string; items: GuideSection[] }[] = [];
    for (const s of shown) {
      const last = out[out.length - 1];
      if (last && last.name === s.group) last.items.push(s);
      else out.push({ name: s.group, items: [s] });
    }
    return out;
  }, [shown]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="flex items-center gap-2 text-lg font-semibold">
          <BookOpen className="h-5 w-5 text-primary" aria-hidden /> Guide
        </h1>
        <label className="relative block w-full max-w-xs">
          <Search className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-fg-muted" aria-hidden />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search the guide…"
            aria-label="Search the guide"
            className="w-full rounded-md border border-line bg-canvas py-1.5 pl-8 pr-3 text-sm placeholder:text-fg-muted hover:border-line-strong focus:border-primary"
          />
        </label>
      </div>

      <div className="flex items-start gap-6">
        <nav aria-label="Guide contents" className="sticky top-4 hidden max-h-[calc(100vh-2rem)] w-56 shrink-0 space-y-4 overflow-y-auto pr-2 lg:block">
          {groups.map((g) => (
            <div key={g.name}>
              <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-fg-muted">{g.name}</p>
              <ul className="space-y-0.5">
                {g.items.map((s) => (
                  <li key={s.id}>
                    <a
                      href={`#${s.id}`}
                      className={`block rounded px-2 py-1 text-[13px] transition-colors ${active === s.id ? "bg-primary/10 text-primary" : "text-fg-nav hover:bg-elevated hover:text-fg"}`}
                    >
                      {s.title}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {shown.length === 0 && <p className="text-xs text-fg-muted">No chapter matches.</p>}
        </nav>

        <div className="min-w-0 max-w-4xl flex-1 space-y-10">
          {!needle && (
            <div className="flex gap-2.5 rounded-md border border-line bg-surface px-3 py-2.5 text-xs leading-relaxed text-fg-muted">
              <Info className="mt-0.5 h-4 w-4 shrink-0 text-telemetry" aria-hidden />
              <p>
                The screenshots come from a demonstration installation with sample hosts, so names, addresses and values are not
                real. Menu entries and buttons are shown only to people whose role has the permission; each chapter says which.
              </p>
            </div>
          )}
          {shown.map((s) => (
            <section key={s.id} id={s.id} className="scroll-mt-4 space-y-3">
              <header className="space-y-1 border-b border-line pb-2">
                <h2 className="text-base font-semibold text-fg">{s.title}</h2>
                <p className="text-sm text-fg-secondary">{s.summary}</p>
                {s.needs && <p className="text-xs text-fg-muted">Needs: {inline(s.needs)}</p>}
              </header>
              <div className="space-y-3">
                {s.blocks.map((b, i) => (
                  <BlockView key={i} block={b} />
                ))}
              </div>
            </section>
          ))}
          {shown.length === 0 && <p className="text-sm text-fg-muted">Nothing in the guide matches “{query}”.</p>}
        </div>
      </div>
    </div>
  );
}
