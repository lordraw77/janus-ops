import type { ReactNode } from "react";

/** The row of filters above a table: one grid, every control on the same line. */
export function FilterBar({ children }: { children: ReactNode }) {
  return (
    <div className="mb-3 grid grid-cols-2 items-end gap-3 md:grid-cols-4 xl:grid-cols-[minmax(14rem,2fr)_repeat(5,minmax(8rem,1fr))]">
      {children}
    </div>
  );
}
