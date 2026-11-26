"use client";

/** Small Obsidian Graphite building blocks shared by the console pages. */

import { LoaderCircle, X } from "lucide-react";
import { type ButtonHTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, useEffect, useRef } from "react";

type Variant = "primary" | "secondary" | "danger";

const BUTTON: Record<Variant, string> = {
  primary: "bg-primary text-canvas shadow-sm hover:bg-primary-hover active:brightness-95",
  secondary: "border border-line bg-elevated text-fg hover:border-line-strong hover:bg-elevated/70 active:brightness-95",
  danger: "border border-crit/40 bg-crit/10 text-crit hover:bg-crit/20 active:bg-crit/30",
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
      className={`inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${BUTTON[variant]} ${className}`}
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
      className={`w-full rounded-md border border-line bg-canvas px-3 py-1.5 text-sm transition-colors placeholder:text-fg-muted hover:border-line-strong focus:border-primary disabled:opacity-60 ${props.className ?? ""}`}
    />
  );
}

export function Card({ title, actions, children }: { title?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-line bg-surface p-4 shadow-sm shadow-black/20">
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

/** A small spinning ring: something is being loaded or done. */
export function Spinner({ className = "" }: { className?: string }) {
  return <LoaderCircle className={`h-4 w-4 shrink-0 animate-spin text-primary ${className}`} aria-hidden />;
}

/** Spinner and a sentence, for a place that is waiting for its data. */
export function Loading({ label = "Loading…", className = "" }: { label?: string; className?: string }) {
  return (
    <p role="status" className={`flex items-center gap-2 text-sm text-fg-muted ${className}`}>
      <Spinner />
      {label}
    </p>
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
    <p role={kind === "error" ? "alert" : "status"} className={`rounded-md border px-3 py-2 text-xs ${style}`}>
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
  huge = false,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  /** logs, terminals */
  wide?: boolean;
  /** editors that need the whole screen (the workflow graph) */
  huge?: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // Escape only works with focus inside: move it in unless a field already took it (autoFocus)
    const prev = document.activeElement as HTMLElement | null;
    if (panel.current && !panel.current.contains(document.activeElement)) panel.current.focus();
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = overflow;
      prev?.focus?.();
    };
  }, []);
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/80 p-4 backdrop-blur-sm"
      onKeyDown={(e) => e.key === "Escape" && onClose()}
    >
      <div ref={panel} tabIndex={-1} className={`max-h-[94vh] w-full overflow-y-auto outline-none ${huge ? "max-w-[96rem]" : wide ? "max-w-5xl" : "max-w-md"} rounded-lg border border-line-strong/60 bg-surface p-5 shadow-2xl shadow-black/50`}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-semibold">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded p-1 text-fg-muted transition-colors hover:bg-elevated hover:text-fg"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

export const SELECT_CLASS =
  "w-full rounded-md border border-line bg-canvas px-3 py-1.5 text-sm transition-colors placeholder:text-fg-muted hover:border-line-strong focus:border-primary";

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
