/** Small Obsidian Graphite building blocks shared by the console pages. */

import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "danger";

const BUTTON: Record<Variant, string> = {
  primary: "bg-primary text-canvas hover:bg-primary-hover",
  secondary: "border border-line bg-elevated text-fg hover:border-line-strong",
  danger: "border border-crit/40 bg-crit/10 text-crit hover:bg-crit/20",
};

export function Button({
  variant = "secondary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type="button"
      {...props}
      className={`inline-flex items-center justify-center gap-1.5 rounded px-3 py-1.5 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-50 ${BUTTON[variant]} ${className}`}
    />
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-fg-secondary">{label}</span>
      {children}
      {hint && <span className="block text-[11px] text-fg-muted">{hint}</span>}
    </label>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`w-full rounded border border-line bg-canvas px-3 py-1.5 text-sm outline-none focus:border-primary disabled:opacity-60 ${props.className ?? ""}`}
    />
  );
}

export function Card({ title, actions, children }: { title?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-line bg-surface p-4">
      {(title || actions) && (
        <div className="mb-3 flex items-center justify-between gap-3">
          {title && (
            <h2 className="text-xs font-semibold uppercase tracking-wider text-fg-muted">{title}</h2>
          )}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

export function Alert({ kind = "error", children }: { kind?: "error" | "info" | "ok"; children: ReactNode }) {
  const style =
    kind === "error"
      ? "border-crit/40 bg-crit/10 text-crit"
      : kind === "ok"
        ? "border-ok/40 bg-ok/10 text-ok"
        : "border-telemetry/40 bg-telemetry/10 text-telemetry";
  return (
    <p role={kind === "error" ? "alert" : "status"} className={`rounded border px-3 py-2 text-xs ${style}`}>
      {children}
    </p>
  );
}

const ROLE_STYLE: Record<string, string> = {
  admin: "border-primary/40 text-primary",
  operator: "border-telemetry/40 text-telemetry",
  auditor: "border-ai/40 text-ai",
};

export function RoleBadge({ role }: { role: string }) {
  return (
    <span
      className={`inline-block rounded border px-1.5 font-mono text-[10px] uppercase ${ROLE_STYLE[role] ?? "border-line-strong text-fg-secondary"}`}
    >
      {role}
    </span>
  );
}

export function Modal({
  title,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  /** logs, terminals */
  wide?: boolean;
}) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/80 p-4"
      onKeyDown={(e) => e.key === "Escape" && onClose()}
    >
      <div className={`max-h-[94vh] w-full overflow-y-auto ${wide ? "max-w-5xl" : "max-w-md"} rounded-lg border border-line bg-surface p-5 shadow-2xl`}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-semibold">{title}</h2>
          <button type="button" onClick={onClose} aria-label="Close" className="text-fg-muted hover:text-fg">
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export const SELECT_CLASS =
  "w-full rounded border border-line bg-canvas px-3 py-1.5 text-sm outline-none focus:border-primary";

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={`${SELECT_CLASS} ${props.className ?? ""}`} />;
}

export function StateBadge({ state, soft }: { state: string | null; soft?: boolean }) {
  const styles: Record<string, string> = {
    ok: "bg-ok text-canvas",
    warn: "bg-warn text-canvas",
    crit: "bg-crit text-white",
    unknown: "bg-unknown text-white",
  };
  return (
    <span
      title={soft ? "Soft state: not confirmed yet (check cycles)" : undefined}
      className={`inline-block min-w-12 rounded px-1.5 py-0.5 text-center font-mono text-[10px] font-semibold ${state ? styles[state] : "bg-offline text-fg"} ${soft ? "opacity-60" : ""}`}
    >
      {state ? state.toUpperCase() : "PENDING"}
    </span>
  );
}
