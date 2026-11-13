"use client";

import { KeyRound, LogIn } from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import { Alert } from "@/components/ui";
import { api, message } from "@/lib/api";
import { fetchSession } from "@/lib/auth";

const INPUT =
  "w-full rounded border border-line bg-canvas px-3 py-2 text-sm outline-none focus:border-primary";
const SUBMIT =
  "flex w-full items-center justify-center gap-2 rounded bg-primary px-3 py-2 text-sm font-semibold text-canvas hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50";

export default function LoginPage() {
  const router = useRouter();
  const [step, setStep] = useState<"password" | "totp">("password");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
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

  async function submitPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const r = await api<{ mfa_required: boolean }>("/auth/login", {
      method: "POST",
      body: { username: username.trim(), password },
    });
    setBusy(false);
    setPassword("");
    if (!r.ok) {
      setError(
        r.code === "invalid_input"
          ? "Enter a valid username (letters, digits, . _ - @) and a password."
          : message(r.code, r.retryAfter),
      );
      return;
    }
    if (r.data.mfa_required) setStep("totp");
    else router.replace("/");
  }

  async function submitCode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const r = await api("/auth/login/totp", { method: "POST", body: { code: code.trim() } });
    setBusy(false);
    setCode("");
    if (r.ok) {
      router.replace("/");
      return;
    }
    if (r.code === "mfa_locked" || r.code === "not_authenticated") setStep("password");
    setError(message(r.code === "invalid_input" ? "invalid_code" : r.code));
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

        {step === "password" ? (
          <form onSubmit={submitPassword} className="space-y-4" noValidate>
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
                className={INPUT}
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
                className={INPUT}
              />
            </div>
            {error && <Alert>{error}</Alert>}
            <button type="submit" disabled={busy || !username.trim() || !password} className={SUBMIT}>
              <LogIn className="h-4 w-4" aria-hidden />
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </form>
        ) : (
          <form onSubmit={submitCode} className="space-y-4" noValidate>
            <p className="text-xs text-fg-secondary">
              Enter the 6-digit code from your authenticator app, or one of your recovery codes.
            </p>
            <div className="space-y-1.5">
              <label htmlFor="code" className="text-xs font-medium text-fg-secondary">
                Verification code
              </label>
              <input
                id="code"
                name="code"
                autoComplete="one-time-code"
                inputMode="text"
                autoFocus
                required
                maxLength={32}
                value={code}
                onChange={(e) => setCode(e.target.value)}
                className={`${INPUT} font-mono tracking-widest`}
              />
            </div>
            {error && <Alert>{error}</Alert>}
            <button type="submit" disabled={busy || code.trim().length < 6} className={SUBMIT}>
              <KeyRound className="h-4 w-4" aria-hidden />
              {busy ? "Verifying…" : "Verify"}
            </button>
            <button
              type="button"
              onClick={() => {
                setStep("password");
                setError(null);
              }}
              className="w-full text-center text-xs text-fg-muted hover:text-fg"
            >
              Use a different account
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
