export type Status = "ok" | "warn" | "crit" | "unknown" | "offline";

const STYLES: Record<Status, string> = {
  ok: "bg-ok text-canvas",
  warn: "bg-warn text-canvas",
  crit: "bg-crit text-white",
  unknown: "bg-unknown text-white",
  offline: "bg-offline text-fg",
};

const LABELS: Record<Status, string> = {
  ok: "OK",
  warn: "WARN",
  crit: "CRIT",
  unknown: "UNKNOWN",
  offline: "OFFLINE",
};

export function StatusBadge({ status }: { status: Status }) {
  return (
    <span
      className={`inline-block min-w-16 rounded px-2 py-0.5 text-center font-mono text-[11px] font-semibold ${STYLES[status]}`}
    >
      {LABELS[status]}
    </span>
  );
}
