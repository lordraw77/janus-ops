"use client";

import { LogIn } from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import { fetchSession } from "@/lib/auth";

const MESSAGES: Record<string, string> = {
  invalid_credentials: "Invalid username or password.",
  no_role: "Your account is not a member of any Janus-Ops group. Ask an administrator.",
  directory_unavailable:
    "The user directory is unavailable. Try again later or use the break-glass account.",
  account_disabled: "This account is disabled.",
  account_conflict: "This username is already linked to another account. Contact an administrator.",
};

function retryMessage(seconds: number): string {
  const minutes = Math.ceil(seconds / 60);
  return `Too many failed attempts. Try again in ${minutes} minute${minutes === 1 ? "" : "s"}.`;
}

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Already signed in: go straight to the console
  useEffect(() => {
    fetchSession()
      .then((user) => {
        if (user) router.replace("/");
      })
      .catch(() => undefined);
  }, [router]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      if (res.ok) {
        router.replace("/");
        return;
      }
      setPassword("");
      if (res.status === 429) {
        setError(retryMessage(Number(res.headers.get("Retry-After") ?? "60")));
      } else if (res.status === 422) {
        setError("Enter a valid username (letters, digits, . _ - @) and a password.");
      } else {
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        setError(MESSAGES[body.detail ?? ""] ?? `Sign-in failed (HTTP ${res.status}).`);
      }
    } catch {
      setError("The Janus-Ops API is not reachable.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <div className="w-full max-w-sm rounded-lg border border-line bg-surface p-6 shadow-2xl">
        <div className="mb-6 flex flex-col items-center gap-2">
          {/* eslint-disable-next-line @next/next/no-img-element -- static export, no optimizer */}
          <img src="/brand/janus-ops-logo-256.png" alt="" className="h-20 w-20 object-contain" />
          <h1 className="text-lg font-bold">
            Janus-<span className="text-primary">Ops</span>
          </h1>
          <p className="font-mono text-[10px] text-fg-muted">NOC CONTROL PLANE</p>
        </div>

        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <div className="space-y-1.5">
            <label htmlFor="username" className="text-xs font-medium text-fg-secondary">
              Username
            </label>
            <input
              id="username"
              name="username"
              autoComplete="username"
              autoFocus
              required
              maxLength={64}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full rounded border border-line bg-canvas px-3 py-2 text-sm outline-none focus:border-primary"
            />
          </div>
          <div className="space-y-1.5">
            <label htmlFor="password" className="text-xs font-medium text-fg-secondary">
              Password
            </label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded border border-line bg-canvas px-3 py-2 text-sm outline-none focus:border-primary"
            />
          </div>

          {error && (
            <p role="alert" className="rounded border border-crit/40 bg-crit/10 px-3 py-2 text-xs text-crit">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy || !username.trim() || !password}
            className="flex w-full items-center justify-center gap-2 rounded bg-primary px-3 py-2 text-sm font-semibold text-canvas hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50"
          >
            <LogIn className="h-4 w-4" aria-hidden />
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
