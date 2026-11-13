"use client";

import { useRouter } from "next/navigation";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from "react";

export type Role = "admin" | "operator" | "auditor";

export type SessionUser = {
  username: string;
  display_name: string | null;
  role: Role;
  method: "ldap" | "breakglass";
};

type SessionResponse = { user: SessionUser; expires_at: string };

type AuthContextValue = {
  user: SessionUser;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export async function fetchSession(): Promise<SessionUser | null> {
  const res = await fetch("/api/v1/auth/me", { cache: "no-store" });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(`session check failed: HTTP ${res.status}`);
  return ((await res.json()) as SessionResponse).user;
}

type GateState = { kind: "checking" } | { kind: "error" } | { kind: "ready"; user: SessionUser };

/** Renders children only for an authenticated user; otherwise redirects to /login/. */
export function AuthGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [state, setState] = useState<GateState>({ kind: "checking" });

  useEffect(() => {
    let cancelled = false;
    fetchSession()
      .then((user) => {
        if (cancelled) return;
        if (user) setState({ kind: "ready", user });
        else router.replace("/login/");
      })
      .catch(() => {
        if (!cancelled) setState({ kind: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  const logout = useCallback(async () => {
    await fetch("/api/v1/auth/logout", { method: "POST" });
    router.replace("/login/");
  }, [router]);

  if (state.kind === "checking") {
    return <div className="p-6 text-sm text-fg-muted">Checking session…</div>;
  }
  if (state.kind === "error") {
    return (
      <div className="p-6 text-sm">
        The Janus-Ops API is not reachable. Reload the page to retry.
      </div>
    );
  }
  return <AuthContext.Provider value={{ user: state.user, logout }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthGate>");
  return ctx;
}
