"use client";

import { ShieldCheck, ShieldOff } from "lucide-react";
import { type FormEvent, useState } from "react";

import { Alert, Button, Card, Field, Loading, RoleBadge, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";

type TotpStatus = { enabled: boolean; recovery_codes_left: number; available: boolean };
type Setup = { secret: string; otpauth_uri: string; qr_svg: string };

function svgDataUri(svg: string): string {
  // Rendered as an <img>: the SVG cannot run scripts or touch the page
  return `data:image/svg+xml;base64,${btoa(svg)}`;
}

function RecoveryCodes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  return (
    <div className="space-y-3">
      <Alert kind="info">
        Save these recovery codes somewhere safe. Each one works once if you lose your phone. They
        are shown only now.
      </Alert>
      <ul className="grid grid-cols-2 gap-1 rounded border border-line bg-canvas p-3 font-mono text-xs">
        {codes.map((c) => (
          <li key={c}>{c}</li>
        ))}
      </ul>
      <div className="flex gap-2">
        <Button onClick={() => void navigator.clipboard?.writeText(codes.join("\n"))}>Copy</Button>
        <Button variant="primary" onClick={onDone}>
          I have saved them
        </Button>
      </div>
    </div>
  );
}

function PasswordCard() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setDone(false);
    if (next !== again) return setError("The two new passwords do not match.");
    setBusy(true);
    const r = await api("/auth/password", {
      method: "POST",
      body: { current_password: current, new_password: next },
    });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    setCurrent("");
    setNext("");
    setAgain("");
    setDone(true);
  }

  return (
    <Card title="Password">
      <form onSubmit={submit} className="space-y-3">
        {error && <Alert>{error}</Alert>}
        {done && <Alert kind="info">Password changed. Your other sessions were signed out.</Alert>}
        <Field label="Current password">
          <TextInput type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
        </Field>
        <Field label="New password" hint="At least 12 characters">
          <TextInput type="password" autoComplete="new-password" required value={next} onChange={(e) => setNext(e.target.value)} />
        </Field>
        <Field label="Repeat new password">
          <TextInput type="password" autoComplete="new-password" required value={again} onChange={(e) => setAgain(e.target.value)} />
        </Field>
        <Button type="submit" variant="primary" disabled={busy || !current || next.length < 12 || !again}>
          Change password
        </Button>
      </form>
    </Card>
  );
}

function TotpCard() {
  const { refresh } = useAuth();
  const { data: status, error: loadError, reload } = useResource<TotpStatus>("/auth/totp");
  const [setup, setSetup] = useState<Setup | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [code, setCode] = useState("");
  const [mode, setMode] = useState<"idle" | "disable" | "regenerate">("idle");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(path: string, body?: unknown) {
    setBusy(true);
    setError(null);
    const r = await api<{ recovery_codes?: string[] } & Partial<Setup>>(path, { method: "POST", body });
    setBusy(false);
    setCode("");
    if (!r.ok) {
      setError(message(r.code === "invalid_input" ? "invalid_code" : r.code));
      return null;
    }
    return r.data;
  }

  async function start() {
    const data = await run("/auth/totp/setup");
    if (data) setSetup(data as Setup);
  }

  async function confirm(event: FormEvent) {
    event.preventDefault();
    const data = await run("/auth/totp/enable", { code });
    if (data?.recovery_codes) {
      setSetup(null);
      setCodes(data.recovery_codes);
      reload();
      refresh();
    }
  }

  async function submitMode(event: FormEvent) {
    event.preventDefault();
    if (mode === "disable") {
      const r = await api("/auth/totp/disable", { method: "POST", body: { code } });
      setCode("");
      if (!r.ok) return setError(message(r.code === "invalid_input" ? "invalid_code" : r.code));
      setMode("idle");
      reload();
      refresh();
    } else {
      const data = await run("/auth/totp/recovery-codes", { code });
      if (data?.recovery_codes) {
        setMode("idle");
        setCodes(data.recovery_codes);
        reload();
      }
    }
  }

  if (!status) return <Card title="Two-factor authentication">{loadError ?? <Loading />}</Card>;

  return (
    <Card title="Two-factor authentication (optional)">
      <div className="space-y-4 text-sm">
        <p className="text-xs text-fg-secondary">
          Time-based codes (TOTP) from any authenticator app. Everything is computed on this
          server and in the app on your phone: no internet access is needed, so it works behind
          corporate proxies and in isolated networks.
        </p>
        {error && <Alert>{error}</Alert>}

        {codes ? (
          <RecoveryCodes codes={codes} onDone={() => setCodes(null)} />
        ) : !status.available ? (
          <Alert kind="info">{message("totp_unavailable")}</Alert>
        ) : status.enabled ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2 text-ok">
              <ShieldCheck className="h-4 w-4" aria-hidden /> Enabled ·{" "}
              <span className="text-xs text-fg-secondary">
                {status.recovery_codes_left} recovery codes left
              </span>
            </div>
            {mode === "idle" ? (
              <div className="flex gap-2">
                <Button onClick={() => setMode("regenerate")}>New recovery codes</Button>
                <Button variant="danger" onClick={() => setMode("disable")}>
                  Turn off
                </Button>
              </div>
            ) : (
              <form onSubmit={submitMode} className="flex items-end gap-2">
                <div className="flex-1">
                  <Field label="Current code or a recovery code">
                    <TextInput
                      autoFocus
                      autoComplete="one-time-code"
                      value={code}
                      onChange={(e) => setCode(e.target.value)}
                      className="font-mono"
                    />
                  </Field>
                </div>
                <Button type="submit" variant={mode === "disable" ? "danger" : "primary"} disabled={busy || code.trim().length < 6}>
                  {mode === "disable" ? "Turn off" : "Generate"}
                </Button>
                <Button onClick={() => setMode("idle")}>Cancel</Button>
              </form>
            )}
          </div>
        ) : setup ? (
          <form onSubmit={confirm} className="space-y-3">
            <p className="text-xs">1. Scan the QR code with your authenticator app.</p>
            {/* eslint-disable-next-line @next/next/no-img-element -- generated locally, data URI */}
            <img src={svgDataUri(setup.qr_svg)} alt="TOTP QR code" className="h-44 w-44 rounded" />
            <p className="text-xs text-fg-secondary">
              Or enter this key manually:{" "}
              <code className="break-all font-mono text-fg">{setup.secret}</code>
            </p>
            <Field label="2. Enter the 6-digit code shown by the app">
              <TextInput
                autoFocus
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                value={code}
                onChange={(e) => setCode(e.target.value)}
                className="w-40 font-mono tracking-widest"
              />
            </Field>
            <div className="flex gap-2">
              <Button type="submit" variant="primary" disabled={busy || code.trim().length !== 6}>
                Turn on
              </Button>
              <Button onClick={() => setSetup(null)}>Cancel</Button>
            </div>
          </form>
        ) : (
          <div className="flex items-center justify-between">
            <span className="flex items-center gap-2 text-fg-secondary">
              <ShieldOff className="h-4 w-4" aria-hidden /> Not enabled
            </span>
            <Button variant="primary" onClick={() => void start()} disabled={busy}>
              Set up
            </Button>
          </div>
        )}
      </div>
    </Card>
  );
}

export default function AccountPage() {
  const { user } = useAuth();
  return (
    <div className="max-w-2xl space-y-4">
      <h1 className="text-lg font-semibold">My account</h1>
      <Card title="Profile">
        <dl className="grid grid-cols-[8rem_1fr] gap-y-2 text-sm">
          <dt className="text-fg-muted">Username</dt>
          <dd className="font-mono">{user.username}</dd>
          <dt className="text-fg-muted">Name</dt>
          <dd>{user.display_name ?? "—"}</dd>
          <dt className="text-fg-muted">Roles</dt>
          <dd className="flex flex-wrap gap-1">
            {user.roles.map((r) => (
              <RoleBadge key={r} role={r} />
            ))}
          </dd>
          <dt className="text-fg-muted">Permissions</dt>
          <dd className="font-mono text-xs text-fg-secondary">
            {user.permissions.length ? user.permissions.join(", ") : "—"}
          </dd>
          <dt className="text-fg-muted">Sign-in</dt>
          <dd>{user.method === "breakglass" ? "Local break-glass account" : "LDAP directory"}</dd>
        </dl>
        <p className="mt-3 text-[11px] text-fg-muted">
          Roles come from your LDAP groups and are read at sign-in.
        </p>
      </Card>
      <PasswordCard />
      <TotpCard />
    </div>
  );
}
