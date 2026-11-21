"use client";

import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";

import type { SortState } from "@/lib/sort";

/** A table header cell that sorts by its column when clicked. */
export function SortTh({
  label,
  col,
  sort,
  onSort,
  className = "py-2 pr-3 font-medium",
}: {
  label: string;
  col: string;
  sort: SortState;
  onSort: (col: string) => void;
  className?: string;
}) {
  const active = sort?.col === col;
  const Icon = !active ? ArrowUpDown : sort.dir === "asc" ? ArrowUp : ArrowDown;
  return (
    <th className={className} aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
      <button type="button" onClick={() => onSort(col)} className={`inline-flex items-center gap-1 hover:text-fg ${active ? "text-fg" : ""}`}>
        {label}
        <Icon className={`h-3 w-3 ${active ? "" : "opacity-40"}`} aria-hidden />
      </button>
    </th>
  );
}
