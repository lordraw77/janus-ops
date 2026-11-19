// Comparing agent versions with the server's (X.Y.Z). Pure, so it is tested.

function parts(v: string): number[] | null {
  const m = /^(\d+)\.(\d+)\.(\d+)/.exec(v.trim());
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

/** True when `agent` is a lower version than `current`; false when equal, newer or unreadable. */
export function isOlder(agent: string | null | undefined, current: string | null | undefined): boolean {
  const a = agent ? parts(agent) : null;
  const c = current ? parts(current) : null;
  if (!a || !c) return false;
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== c[i]) return a[i] < c[i];
  }
  return false;
}
