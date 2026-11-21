"use client";

import { useRouter } from "next/navigation";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from "react";

export type Permission =
  | "audit.read"
  | "users.read"
  | "users.manage"
  | "roles.manage"
  | "inventory.read"
  | "inventory.manage"
  | "credentials.manage"
  | "ssh.connect"
  | "sftp.transfer"
  | "snippets.manage"
  | "agents.manage"
  | "monitoring.manage"
  | "notifications.manage"
  | "containers.manage"
  | "containers.exec"
  | "environments.manage"
  | "ai.manage"
  | "proxmox.manage"
  | "proxmox.console"
  | "ansible.read"
  | "ansible.manage"
  | "ansible.run"
  | "ai.use"
  | "ai.approve"
  | "actions.execute"
  | "ai.operate"
  | "archive.read"
  | "archive.manage"
  | "gitops.manage"
  | "gitops.deploy"
  | "workflow.manage"
  | "workflow.run"
  | "workflow.approve";

export type SessionUser = {
  username: string;
  display_name: string | null;
  roles: string[];
  permissions: Permission[];
  method: "ldap" | "breakglass";
  totp_enabled: boolean;
};

type SessionResponse = { user: SessionUser; expires_at: string };

type AuthContextValue = {
  user: SessionUser;
  can: (permission: Permission) => boolean;
  refresh: () => void;
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
  const [version, setVersion] = useState(0);

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
  }, [router, version]);

  const refresh = useCallback(() => setVersion((v) => v + 1), []);

  const logout = useCallback(async () => {
    await fetch("/api/v1/auth/logout", { method: "POST" });
    router.replace("/login/");
  }, [router]);

  if (state.kind === "checking") {
    return <div className="p-6 text-sm text-fg-muted">Checking session…</div>;
  }
  if (state.kind === "error") {
    return (
      <div className="p-6 text-sm">The Janus-Ops API is not reachable. Reload the page to retry.</div>
    );
  }
  const value: AuthContextValue = {
    user: state.user,
    can: (permission) => state.user.permissions.includes(permission),
    refresh,
    logout,
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthGate>");
  return ctx;
}

/** Page-level guard: shows a notice instead of the page when a permission is missing. */
export function RequirePermission({ permission, children }: { permission: Permission; children: ReactNode }) {
  const { can } = useAuth();
  if (!can(permission)) {
    return (
      <div className="rounded-lg border border-line bg-surface p-4 text-sm text-fg-secondary">
        You do not have access to this page. Ask an administrator for the{" "}
        <code className="font-mono text-xs">{permission}</code> permission.
      </div>
    );
  }
  return <>{children}</>;
}
