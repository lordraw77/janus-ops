import { useMemo, useState } from "react";

export type SortState = { col: string; dir: "asc" | "desc" } | null;
export type Cell = string | number | boolean | null | undefined;
/** How to read the sortable value of each column from a row. Define it once, outside the component. */
export type Getters<T> = Record<string, (row: T) => Cell>;

/** Click: ascending, click again: descending, a third time: back to the original order. */
export function nextSort(cur: SortState, col: string): SortState {
  if (cur?.col !== col) return { col, dir: "asc" };
  return cur.dir === "asc" ? { col, dir: "desc" } : null;
}

function compare(a: Cell, b: Cell): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

/** A sorted copy (stable). Empty values always go last, whichever the direction. */
export function sortRows<T>(rows: readonly T[], sort: SortState, getters: Getters<T>): T[] {
  const get = sort ? getters[sort.col] : undefined;
  if (!sort || !get) return [...rows];
  const sign = sort.dir === "asc" ? 1 : -1;
  return rows
    .map((row, i) => ({ row, i, v: get(row) }))
    .sort((x, y) => {
      const ex = x.v === null || x.v === undefined || x.v === "";
      const ey = y.v === null || y.v === undefined || y.v === "";
      if (ex || ey) return ex === ey ? x.i - y.i : ex ? 1 : -1;
      return compare(x.v, y.v) * sign || x.i - y.i;
    })
    .map((x) => x.row);
}

export function useSortedRows<T>(rows: readonly T[], getters: Getters<T>, initial: SortState = null) {
  const [sort, setSort] = useState<SortState>(initial);
  // `getters` is defined once at module level, so it never changes between renders
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const sorted = useMemo(() => sortRows(rows, sort, getters), [rows, sort]);
  return { sorted, sort, toggle: (col: string) => setSort((cur) => nextSort(cur, col)) };
}
