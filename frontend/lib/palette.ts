// The command palette (Ctrl+K): what can be reached and how a typed query ranks it. Pure, so it is tested.

export type Entry = {
  id: string;
  /** what the user reads and types against */
  label: string;
  /** dimmer second line: where it goes */
  hint?: string;
  /** where Enter goes; an entry with an `act` does something instead */
  href?: string;
  act?: { kind: "run-check"; checkId: string };
  group: "Go to" | "Nodes" | "Actions";
  /** extra words that should also match (not shown) */
  keywords?: string;
};

const MAX = 12;

const words = (s: string) => s.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);

/** Every word typed must start a word of the entry (label first, then hint and keywords).
 * A match in the label beats one in the hint; a match at the very start of the label beats the rest. */
export function score(entry: Entry, query: string): number {
  const q = words(query);
  if (q.length === 0) return 0;
  const inLabel = words(entry.label);
  const inRest = words(`${entry.hint ?? ""} ${entry.keywords ?? ""}`);
  let total = 0;
  for (const w of q) {
    if (inLabel.some((x) => x.startsWith(w))) total += inLabel[0]?.startsWith(w) ? 3 : 2;
    else if (inRest.some((x) => x.startsWith(w))) total += 1;
    else return -1;
  }
  return total;
}

/** With no query: the pages, in their order. With one: the best matches first, at most MAX. */
export function rank(entries: Entry[], query: string): Entry[] {
  if (words(query).length === 0) return entries.filter((e) => e.group === "Go to").slice(0, MAX);
  return entries
    .map((e, i) => ({ e, s: score(e, query), i }))
    .filter((x) => x.s >= 0)
    .sort((a, b) => b.s - a.s || a.i - b.i)
    .slice(0, MAX)
    .map((x) => x.e);
}

/** Wraps around at both ends. */
export function move(index: number, by: 1 | -1, length: number): number {
  return length === 0 ? 0 : (index + by + length) % length;
}
