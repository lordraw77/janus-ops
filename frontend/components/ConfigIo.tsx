"use client";

import { Download, Upload } from "lucide-react";
import { useState } from "react";

import { Alert, Button, Card, Field, Spinner, TextInput } from "@/components/ui";
import { api, message } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const SECTIONS: { id: string; label: string; hint: string }[] = [
  { id: "environments", label: "Environments", hint: "prod, staging, …" },
  { id: "credentials", label: "Credentials", hint: "names and kinds; the secrets only if you include them" },
  { id: "nodes", label: "Hosts", hint: "address, tags, capabilities, services, parent" },
  { id: "groups", label: "Groups", hint: "and their members" },
  { id: "templates", label: "Check templates", hint: "your own; the built-in ones come with the product" },
  { id: "checks", label: "Checks", hint: "every check of every host" },
  { id: "snmp_oids", label: "SNMP OID catalog", hint: "" },
  { id: "notifications", label: "Notifications", hint: "channels and rules" },
  { id: "snippets", label: "Shared snippets", hint: "" },
  { id: "workflows", label: "Workflows", hint: "" },
  { id: "ai_providers", label: "AI providers", hint: "models and routes; the API keys only if you include secrets" },
];

const CODES: Record<string, string> = {
  passphrase_required: "A passphrase is needed.",
  passphrase_too_short: "The passphrase needs at least 12 characters.",
  wrong_passphrase: "Wrong passphrase.",
  not_a_config_file: "This is not a Janus-Ops configuration file.",
  unsupported_version: "This file comes from a newer version of Janus-Ops.",
  unsupported_secrets: "The secrets block of this file cannot be read.",
  unknown_section: "Choose at least one section.",
  vault_unavailable: "The vault is not available.",
};
const msg = (code: string) => CODES[code] ?? message(code);

type SectionReport = { created: number; updated: number; skipped: number; failed: { name: string; error: string }[]; notes: string[] };
type Report = { dry_run: boolean; mode: string; report: Record<string, SectionReport> };
type Doc = {
  format: string;
  exported_at?: string;
  app_version?: string;
  sections: Record<string, unknown>;
  secrets?: unknown;
};

function countOf(value: unknown): number {
  if (Array.isArray(value)) return value.length;
  if (value && typeof value === "object") return Object.values(value).reduce<number>((n, v) => n + countOf(v), 0);
  return 0;
}

const CHECKBOX = "h-4 w-4 accent-primary";

function ExportCard() {
  const { can } = useAuth();
  const [picked, setPicked] = useState<Set<string>>(new Set(SECTIONS.map((s) => s.id)));
  const [withSecrets, setWithSecrets] = useState(false);
  const [pass, setPass] = useState("");
  const [pass2, setPass2] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const canSecrets = can("credentials.reveal");

  const toggle = (id: string) =>
    setPicked((cur) => {
      const next = new Set(cur);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  const passOk = !withSecrets || (pass.length >= 12 && pass === pass2);

  async function run() {
    setBusy(true);
    setError(null);
    setDone(null);
    const r = await api<Record<string, unknown>>("/config/export", {
      method: "POST",
      body: { sections: [...picked], include_secrets: withSecrets, passphrase: withSecrets ? pass : null },
    });
    setBusy(false);
    if (!r.ok) return setError(msg(r.code));
    const blob = new Blob([JSON.stringify(r.data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `janus-ops-config-${new Date().toISOString().slice(0, 10)}${withSecrets ? "-with-secrets" : ""}.json`;
    a.click();
    URL.revokeObjectURL(url);
    setDone(`Downloaded: ${countOf(r.data.sections)} items${withSecrets ? ", secrets encrypted with your passphrase" : ""}.`);
    setPass("");
    setPass2("");
  }

  return (
    <Card title="Export">
      <div className="space-y-3 text-sm">
        <p className="text-xs text-fg-secondary">
          Downloads one file with the configuration you choose. Ids are written as names, so the file can be imported into another Janus-Ops. Users and
          roles (they come from the directory), the audit log, metrics, agents, GitOps stacks and Ansible sources are not included.
        </p>
        <div className="grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
          {SECTIONS.map((s) => (
            <label key={s.id} className="flex items-start gap-2 text-sm">
              <input type="checkbox" className={`${CHECKBOX} mt-0.5`} checked={picked.has(s.id)} onChange={() => toggle(s.id)} />
              <span>
                {s.label}
                {s.hint && <span className="block text-[11px] text-fg-muted">{s.hint}</span>}
              </span>
            </label>
          ))}
        </div>
        <div className="space-y-2 rounded-md border border-line p-3">
          <label className={`flex items-start gap-2 text-sm ${canSecrets ? "" : "opacity-50"}`}>
            <input type="checkbox" className={`${CHECKBOX} mt-0.5`} disabled={!canSecrets} checked={withSecrets} onChange={(e) => setWithSecrets(e.target.checked)} />
            <span>
              Include secrets (passwords, keys, tokens, API keys)
              <span className="block text-[11px] text-fg-muted">
                {canSecrets
                  ? "Encrypted in the file with a passphrase you choose. Without the passphrase the file cannot be restored; the passphrase is not stored."
                  : "Needs the credentials.reveal permission."}
              </span>
            </span>
          </label>
          {withSecrets && (
            <div className="grid gap-2 sm:grid-cols-2">
              <Field label="Passphrase" hint="At least 12 characters">
                <TextInput type="password" autoComplete="new-password" value={pass} onChange={(e) => setPass(e.target.value)} />
              </Field>
              <Field label="Repeat the passphrase">
                <TextInput type="password" autoComplete="new-password" value={pass2} onChange={(e) => setPass2(e.target.value)} />
              </Field>
            </div>
          )}
        </div>
        {error && <Alert>{error}</Alert>}
        {done && <Alert kind="ok">{done}</Alert>}
        <Button variant="primary" disabled={busy || picked.size === 0 || !passOk} onClick={() => void run()}>
          {busy ? <Spinner className="h-3.5 w-3.5 text-canvas" /> : <Download className="h-3.5 w-3.5" aria-hidden />} Download
        </Button>
      </div>
    </Card>
  );
}

function ReportTable({ report }: { report: Report }) {
  const rows = Object.entries(report.report);
  return (
    <div className="space-y-2">
      <table className="w-full text-left text-xs">
        <thead className="text-fg-muted">
          <tr>
            <th className="py-1 pr-3 font-medium">Section</th>
            <th className="py-1 pr-3 font-medium">{report.dry_run ? "Would create" : "Created"}</th>
            <th className="py-1 pr-3 font-medium">{report.dry_run ? "Would update" : "Updated"}</th>
            <th className="py-1 pr-3 font-medium">Skipped</th>
            <th className="py-1 font-medium">Failed</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {rows.map(([name, r]) => (
            <tr key={name}>
              <td className="py-1 pr-3">{SECTIONS.find((s) => s.id === name)?.label ?? name}</td>
              <td className="py-1 pr-3 font-mono">{r.created}</td>
              <td className="py-1 pr-3 font-mono">{r.updated}</td>
              <td className="py-1 pr-3 font-mono text-fg-muted">{r.skipped}</td>
              <td className={`py-1 font-mono ${r.failed.length ? "text-crit" : "text-fg-muted"}`}>{r.failed.length}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.flatMap(([name, r]) => [
        ...r.failed.map((f) => (
          <p key={`${name}-f-${f.name}`} className="text-[11px] text-crit">
            {name}: {f.name} — {f.error}
          </p>
        )),
        ...[...new Set(r.notes)].slice(0, 8).map((n) => (
          <p key={`${name}-n-${n}`} className="text-[11px] text-warn">
            {name}: {n}
          </p>
        )),
      ])}
    </div>
  );
}

function ImportCard() {
  const [doc, setDoc] = useState<Doc | null>(null);
  const [fileName, setFileName] = useState("");
  const [pass, setPass] = useState("");
  const [mode, setMode] = useState<"skip" | "overwrite">("skip");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<Report | null>(null);
  const [applied, setApplied] = useState<Report | null>(null);

  async function load(file: File | undefined) {
    setPreview(null);
    setApplied(null);
    setError(null);
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text()) as Doc;
      if (parsed.format !== "janus-ops-config" || typeof parsed.sections !== "object") throw new Error("format");
      setDoc(parsed);
      setFileName(file.name);
      setPicked(new Set(Object.keys(parsed.sections)));
    } catch {
      setDoc(null);
      setError(CODES.not_a_config_file);
    }
  }

  const togglePick = (id: string) => {
    setPreview(null);
    setPicked((cur) => {
      const next = new Set(cur);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  };

  async function run(dry: boolean) {
    if (!doc) return;
    if (!dry && !window.confirm(`Apply the import now (${mode === "overwrite" ? "overwriting what exists" : "adding what is missing"})?`)) return;
    setBusy(true);
    setError(null);
    const r = await api<Report>("/config/import", {
      method: "POST",
      body: { document: doc, passphrase: doc.secrets ? pass : null, mode, dry_run: dry, sections: [...picked] },
    });
    setBusy(false);
    if (!r.ok) return setError(msg(r.code));
    if (dry) {
      setPreview(r.data);
      setApplied(null);
    } else {
      setApplied(r.data);
      setPreview(null);
    }
  }

  const present = SECTIONS.filter((s) => doc && s.id in doc.sections);
  const needsPass = !!doc?.secrets;
  const ready = !!doc && picked.size > 0 && (!needsPass || pass.length > 0);
  const worst = preview ? Object.values(preview.report).reduce((n, r) => n + r.failed.length, 0) : 0;

  return (
    <Card title="Import">
      <div className="space-y-3 text-sm">
        <p className="text-xs text-fg-secondary">
          Choose a file, check what it would do with <span className="font-semibold">Preview</span> (nothing is changed), then apply it. Items are matched
          by name. Each item is imported on its own: one that fails is listed and the rest goes on.
        </p>
        <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-line bg-elevated px-3 py-1.5 text-xs font-semibold transition-colors hover:border-line-strong focus-within:outline-2">
          <Upload className="h-3.5 w-3.5" aria-hidden /> Choose a file
          <input type="file" accept=".json,application/json" className="sr-only" onChange={(e) => void load(e.target.files?.[0])} />
        </label>
        {error && <Alert>{error}</Alert>}
        {doc && (
          <div className="space-y-3">
            <p className="text-xs text-fg-muted">
              {fileName} · exported {doc.exported_at ? new Date(doc.exported_at).toLocaleString() : "?"}
              {doc.app_version ? ` · Janus-Ops ${doc.app_version}` : ""} · {countOf(doc.sections)} items
              {needsPass ? " · contains encrypted secrets" : " · no secrets"}
            </p>
            <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
              {present.map((s) => (
                <label key={s.id} className="flex items-center gap-2 text-sm">
                  <input type="checkbox" className={CHECKBOX} checked={picked.has(s.id)} onChange={() => togglePick(s.id)} />
                  {s.label} <span className="font-mono text-[11px] text-fg-muted">{countOf(doc.sections[s.id])}</span>
                </label>
              ))}
            </div>
            {needsPass && (
              <Field label="Passphrase of the file">
                <TextInput type="password" autoComplete="off" value={pass} onChange={(e) => { setPass(e.target.value); setPreview(null); }} />
              </Field>
            )}
            <fieldset className="space-y-1">
              <legend className="mb-1 text-xs font-medium text-fg-secondary">When an item already exists</legend>
              <label className="flex items-center gap-2 text-sm">
                <input type="radio" className={CHECKBOX} checked={mode === "skip"} onChange={() => { setMode("skip"); setPreview(null); }} />
                Leave it as it is (add only what is missing)
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="radio" className={CHECKBOX} checked={mode === "overwrite"} onChange={() => { setMode("overwrite"); setPreview(null); }} />
                Overwrite it with the file
              </label>
            </fieldset>
            <div className="flex flex-wrap items-center gap-2">
              <Button disabled={busy || !ready} onClick={() => void run(true)}>
                {busy && <Spinner className="h-3.5 w-3.5" />} Preview
              </Button>
              <Button variant="primary" disabled={busy || !preview} onClick={() => void run(false)}>
                Apply the import
              </Button>
              {preview && worst > 0 && <span className="text-xs text-warn">{worst} item(s) would fail; the others are imported.</span>}
            </div>
            {preview && (
              <div className="rounded-md border border-line p-3">
                <p className="mb-2 text-xs font-semibold">Preview: nothing has been changed yet</p>
                <ReportTable report={preview} />
              </div>
            )}
            {applied && (
              <div className="rounded-md border border-ok/40 p-3">
                <p className="mb-2 text-xs font-semibold text-ok">Import done</p>
                <ReportTable report={applied} />
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

export function ConfigIoTab() {
  const { can } = useAuth();
  return (
    <div className="space-y-4">
      {can("config.export") && <ExportCard />}
      {can("config.import") && <ImportCard />}
    </div>
  );
}
