"use client";

import { useCallback, useMemo, useRef, useState } from "react";

import { formatValue } from "@/lib/agents";

/** Categorical slots for the dark surface, fixed order (validated: dataviz palette check). */
export const SERIES_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500"];

export type LineSeries = { key: string; label: string; points: { t: number; v: number }[] };

const PAD = { top: 8, right: 8, bottom: 20, left: 72 };
const HEIGHT = 150;

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const exp = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * exp >= v) return m * exp;
  return 10 * exp;
}

function timeLabel(t: number, spanMs: number): string {
  const d = new Date(t);
  if (spanMs > 2 * 86400_000) return d.toLocaleDateString([], { day: "2-digit", month: "2-digit" });
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * One measure over time: thin lines, crosshair and tooltip on hover, legend for 2+ series.
 * A gap longer than three buckets breaks the line (no data, e.g. agent offline).
 */
export function LineChart({
  title,
  series,
  unit,
  bucketS,
  fixedMax,
}: {
  title: string;
  series: LineSeries[];
  unit: string | null;
  bucketS: number;
  fixedMax?: number;
}) {
  const [width, setWidth] = useState(600);
  const [hover, setHover] = useState<number | null>(null);
  const observer = useRef<ResizeObserver | null>(null);
  const measure = useCallback((el: HTMLDivElement | null) => {
    observer.current?.disconnect();
    if (!el) return;
    observer.current = new ResizeObserver(([entry]) => setWidth(Math.max(200, entry.contentRect.width)));
    observer.current.observe(el);
  }, []);

  const shown = series.slice(0, SERIES_COLORS.length);
  const times = useMemo(
    () => [...new Set(shown.flatMap((s) => s.points.map((p) => p.t)))].sort((a, b) => a - b),
    [shown],
  );
  const empty = times.length === 0;
  const t0 = times[0] ?? 0;
  const t1 = times[times.length - 1] ?? 1;
  const maxV = fixedMax ?? niceMax(Math.max(0, ...shown.flatMap((s) => s.points.map((p) => p.v))));
  const plotW = width - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const x = (t: number) => PAD.left + (t1 === t0 ? plotW / 2 : ((t - t0) / (t1 - t0)) * plotW);
  const y = (v: number) => PAD.top + plotH - (Math.min(v, maxV) / maxV) * plotH;

  const paths = shown.map((s) => {
    let d = "";
    let prev: number | null = null;
    for (const p of s.points) {
      const gap = prev !== null && p.t - prev > 3 * bucketS * 1000;
      d += `${d === "" || gap ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`;
      prev = p.t;
    }
    return d;
  });

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    if (empty) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * width;
    let best = times[0];
    for (const t of times) if (Math.abs(x(t) - px) < Math.abs(x(best) - px)) best = t;
    setHover(best);
  }

  const hoverValues =
    hover === null
      ? []
      : shown.map((s, i) => ({ s, i, p: s.points.find((p) => p.t === hover) })).filter((h) => h.p);

  return (
    <div className="rounded-lg border border-line bg-surface p-3">
      <div className="mb-1 flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold text-fg-secondary">{title}</h3>
        {shown.length > 1 && (
          <ul className="flex flex-wrap justify-end gap-x-3 gap-y-0.5 text-[11px] text-fg-secondary">
            {shown.map((s, i) => (
              <li key={s.key} className="flex items-center gap-1">
                <span className="inline-block h-0.5 w-3 rounded" style={{ background: SERIES_COLORS[i] }} />
                {s.label}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div ref={measure} className="relative">
        {empty ? (
          <div className="flex items-center justify-center text-xs text-fg-muted" style={{ height: HEIGHT }}>
            No data in this range
          </div>
        ) : (
          <svg
            width={width}
            height={HEIGHT}
            role="img"
            aria-label={`${title} chart`}
            onPointerMove={onMove}
            onPointerLeave={() => setHover(null)}
            className="block touch-none"
          >
            {[0, 0.5, 1].map((f) => (
              <g key={f}>
                <line
                  x1={PAD.left}
                  x2={width - PAD.right}
                  y1={y(maxV * f)}
                  y2={y(maxV * f)}
                  stroke="var(--color-line)"
                  strokeWidth={1}
                />
                <text
                  x={PAD.left - 6}
                  y={y(maxV * f) + 3}
                  textAnchor="end"
                  className="fill-fg-muted font-mono text-[10px]"
                >
                  {formatValue(maxV * f, unit)}
                </text>
              </g>
            ))}
            {[t0, (t0 + t1) / 2, t1].map((t, i) => (
              <text
                key={i}
                x={x(t)}
                y={HEIGHT - 5}
                textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"}
                className="fill-fg-muted font-mono text-[10px]"
              >
                {timeLabel(t, t1 - t0)}
              </text>
            ))}
            {paths.map((d, i) => (
              <path
                key={shown[i].key}
                d={d}
                fill="none"
                stroke={SERIES_COLORS[i]}
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            ))}
            {hover !== null && (
              <g>
                <line
                  x1={x(hover)}
                  x2={x(hover)}
                  y1={PAD.top}
                  y2={PAD.top + plotH}
                  stroke="var(--color-fg-muted)"
                  strokeWidth={1}
                />
                {hoverValues.map(({ s, i, p }) => (
                  <circle
                    key={s.key}
                    cx={x(hover)}
                    cy={y(p!.v)}
                    r={4}
                    fill={SERIES_COLORS[i]}
                    stroke="var(--color-surface)"
                    strokeWidth={2}
                  />
                ))}
              </g>
            )}
          </svg>
        )}
        {hover !== null && hoverValues.length > 0 && (
          <div
            className="pointer-events-none absolute top-1 z-10 rounded border border-line-strong bg-elevated px-2 py-1 text-[11px] shadow-lg"
            style={x(hover) > width / 2 ? { right: width - x(hover) + 8 } : { left: x(hover) + 8 }}
          >
            <div className="mb-0.5 font-mono text-fg-muted">{new Date(hover).toLocaleString()}</div>
            {hoverValues.map(({ s, i, p }) => (
              <div key={s.key} className="flex items-center gap-2 text-fg">
                <span className="inline-block h-2 w-2 rounded-full" style={{ background: SERIES_COLORS[i] }} />
                <span className="text-fg-secondary">{s.label}</span>
                <span className="ml-auto font-mono tabular-nums">{formatValue(p!.v, unit)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      {series.length > SERIES_COLORS.length && (
        <p className="mt-1 text-[10px] text-fg-muted">
          Showing {SERIES_COLORS.length} of {series.length} series.
        </p>
      )}
    </div>
  );
}
