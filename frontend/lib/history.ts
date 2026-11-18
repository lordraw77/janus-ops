// Undo and redo for an editor that replaces its whole value on every change. Pure, so it is tested.

export type History<T> = {
  past: T[];
  present: T;
  future: T[];
  /** when the present value was last changed (ms): changes that come close together are one step */
  at: number;
};

export const LIMIT = 100;
/** typing in a field changes the value on every key: those are one undo step, not one per letter */
export const BURST_MS = 700;

export const fresh = <T,>(value: T): History<T> => ({ past: [], present: value, future: [], at: -Infinity });

/** The editor changed the value. The same object again is not a change. */
export function push<T>(h: History<T>, next: T, now: number): History<T> {
  if (Object.is(next, h.present)) return h;
  if (h.past.length > 0 && now - h.at < BURST_MS && h.future.length === 0) {
    return { ...h, present: next, at: now };
  }
  return { past: [...h.past, h.present].slice(-LIMIT), present: next, future: [], at: now };
}

export const canUndo = <T,>(h: History<T>) => h.past.length > 0;
export const canRedo = <T,>(h: History<T>) => h.future.length > 0;

export function undo<T>(h: History<T>): History<T> {
  if (!canUndo(h)) return h;
  return { past: h.past.slice(0, -1), present: h.past[h.past.length - 1], future: [h.present, ...h.future], at: -Infinity };
}

export function redo<T>(h: History<T>): History<T> {
  if (!canRedo(h)) return h;
  return { past: [...h.past, h.present], present: h.future[0], future: h.future.slice(1), at: -Infinity };
}
