"use client";

import { KeyRound, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import { type FormEvent, useState } from "react";

import { Alert, Button, Card, Field, Modal, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { RequirePermission, useAuth } from "@/lib/auth";
import { type Credential, KIND_LABELS } from "@/lib/inventory";

const USERNAME_HINT: Record<string, string> = {
  ssh_key: "Default SSH user, e.g. root",
  ssh_password: "SSH user",
  proxmox_token: "Token id, e.g. root@pam!janus",
  proxmox_password: "User with realm, e.g. root@pam or admin@pve",
  fritzbox_password: "FRITZ!Box user, e.g. janus",
  docker_registry: "Registry user",
  snmp_v3: "SNMPv3 user",
};

const SECRET_LABEL: Record<string, string> = {
  ssh_key: "Private key (OpenSSH or PEM)",
  proxmox_token: "Token secret",
  proxmox_password: "Password",
  fritzbox_password: "Password of the FRITZ!Box user",
  generic: "Secret (e.g. SNMP v2c community)",
  snmp_v3: "Authentication passphrase (8+ characters)",
};

type Dialog = { kind: "create" } | { kind: "secret"; cred: Credential } | { kind: "edit"; cred: Credential } | null;

function SecretFields({
  kind,
  secret,
  passphrase,
  onSecret,
  onPassphrase,
}: {
  kind: string;
  secret: string;
  passphrase: string;
  onSecret: (v: string) => void;
  onPassphrase: (v: string) => void;
}) {
  return (
    <>
      <Field label={SECRET_LABEL[kind] ?? "Secret"} hint="Stored encrypted. It is never shown again.">
        {kind === "ssh_key" ? (
          <textarea
            required
            rows={6}
            spellCheck={false}
            value={secret}
            onChange={(e) => onSecret(e.target.value)}
            placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
            className="w-full rounded border border-line bg-canvas px-3 py-1.5 font-mono text-[11px] outline-none focus:border-primary"
          />
        ) : (
          <TextInput type="password" autoComplete="new-password" required value={secret} onChange={(e) => onSecret(e.target.value)} />
        )}
      </Field>
      {kind === "ssh_key" && (
        <Field label="Key passphrase" hint="Only if the key is encrypted">
          <TextInput type="password" autoComplete="off" value={passphrase} onChange={(e) => onPassphrase(e.target.value)} />
        </Field>
      )}
      {kind === "snmp_v3" && (
        <Field label="Privacy passphrase" hint="8+ characters; encrypts the SNMP traffic">
          <TextInput type="password" autoComplete="new-password" required value={passphrase} onChange={(e) => onPassphrase(e.target.value)} />
        </Field>
      )}
    </>
  );
}

function CreateDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [kind, setKind] = useState("ssh_key");
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [description, setDescription] = useState("");
  const [secret, setSecret] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const r = await api("/credentials", {
      method: "POST",
      body: {
        kind,
        name: name.trim(),
        username: username.trim() || null,
        description: description.trim() || null,
        secret,
        passphrase: (kind === "ssh_key" || kind === "snmp_v3") && passphrase ? passphrase : null,
      },
    });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    onDone();
  }

  return (
    <Modal title="New credential" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Type">
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            className="w-full rounded border border-line bg-canvas px-3 py-1.5 text-sm outline-none focus:border-primary"
          >
            {Object.entries(KIND_LABELS).map(([k, label]) => (
              <option key={k} value={k}>
                {label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Name">
          <TextInput autoFocus required value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. prod-root-key" />
        </Field>
        <Field label="Username" hint={USERNAME_HINT[kind]}>
          <TextInput value={username} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <SecretFields kind={kind} secret={secret} passphrase={passphrase} onSecret={setSecret} onPassphrase={setPassphrase} />
        <Field label="Description">
          <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy || !name.trim() || !secret}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function ReplaceSecretDialog({ cred, onClose, onDone }: { cred: Credential; onClose: () => void; onDone: () => void }) {
  const [secret, setSecret] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    const r = await api(`/credentials/${cred.id}/secret`, {
      method: "PUT",
      body: { secret, passphrase: (cred.kind === "ssh_key" || cred.kind === "snmp_v3") && passphrase ? passphrase : null },
    });
    if (!r.ok) return setError(message(r.code));
    onDone();
  }
  return (
    <Modal title={`Replace secret of ${cred.name}`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <SecretFields kind={cred.kind} secret={secret} passphrase={passphrase} onSecret={setSecret} onPassphrase={setPassphrase} />
        {cred.used_by.length > 0 && (
          <p className="text-xs text-fg-secondary">Used by {cred.used_by.length} node(s): the new secret applies to all.</p>
        )}
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={!secret}>
            Replace
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function EditDialog({ cred, onClose, onDone }: { cred: Credential; onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState(cred.name);
  const [username, setUsername] = useState(cred.username ?? "");
  const [description, setDescription] = useState(cred.description ?? "");
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    const r = await api(`/credentials/${cred.id}`, {
      method: "PATCH",
      body: { name: name.trim(), username: username.trim() || null, description: description.trim() || null },
    });
    if (!r.ok) return setError(message(r.code));
    onDone();
  }
  return (
    <Modal title={`Edit ${cred.name}`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Name">
          <TextInput required value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Username" hint={USERNAME_HINT[cred.kind]}>
          <TextInput value={username} onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label="Description">
          <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={!name.trim()}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function CredentialsList() {
  const { can } = useAuth();
  const manage = can("credentials.manage");
  const { data, error, reload } = useResource<Credential[]>("/credentials");
  const [dialog, setDialog] = useState<Dialog>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  function done() {
    setDialog(null);
    reload();
  }

  async function remove(c: Credential) {
    if (!window.confirm(`Delete credential "${c.name}"? This cannot be undone.`)) return;
    const r = await api(`/credentials/${c.id}`, { method: "DELETE" });
    setActionError(r.ok ? null : message(r.code));
    reload();
  }

  return (
    <div className="max-w-6xl space-y-4">
      <h1 className="flex items-center gap-2 text-lg font-semibold">
        <KeyRound className="h-5 w-5 text-primary" aria-hidden /> Credentials
      </h1>
      <Card
        title="Vault"
        actions={
          manage ? (
            <Button variant="primary" onClick={() => setDialog({ kind: "create" })}>
              <Plus className="h-3.5 w-3.5" aria-hidden /> New credential
            </Button>
          ) : null
        }
      >
        <p className="mb-3 text-xs text-fg-secondary">
          Secrets are encrypted (AES-256-GCM, one key per credential) and write-only: nobody can
          read them back from the UI or the API. Janus-Ops uses them server-side to reach your
          nodes; private keys never reach the browser.
        </p>
        {(error || actionError) && <Alert>{error ?? actionError}</Alert>}
        {data && data.length === 0 && <p className="text-sm text-fg-muted">No credentials yet.</p>}
        {data && data.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-line text-fg-muted">
                <tr>
                  <th className="py-2 pr-3 font-medium">Name</th>
                  <th className="py-2 pr-3 font-medium">Type</th>
                  <th className="py-2 pr-3 font-medium">Username</th>
                  <th className="py-2 pr-3 font-medium">Key</th>
                  <th className="py-2 pr-3 font-medium">Used by</th>
                  <th className="py-2 pr-3 font-medium">Updated</th>
                  <th />
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {data.map((c) => (
                  <tr key={c.id}>
                    <td className="py-2 pr-3">
                      <div className="font-semibold">{c.name}</div>
                      {c.description && <div className="text-[11px] text-fg-muted">{c.description}</div>}
                    </td>
                    <td className="py-2 pr-3">{KIND_LABELS[c.kind] ?? c.kind}</td>
                    <td className="py-2 pr-3 font-mono">{c.username ?? ""}</td>
                    <td className="py-2 pr-3 font-mono text-[11px] text-fg-secondary">
                      {c.public_info.key_type && (
                        <>
                          <div>{c.public_info.key_type}</div>
                          <div className="text-fg-muted">{c.public_info.fingerprint}</div>
                        </>
                      )}
                    </td>
                    <td className="py-2 pr-3">
                      {c.used_by.length ? c.used_by.map((u) => `${u.node_name} (${u.capability})`).join(", ") : (
                        <span className="text-fg-muted">unused</span>
                      )}
                    </td>
                    <td className="py-2 pr-3 text-fg-secondary">{new Date(c.updated_at).toLocaleString()}</td>
                    <td className="py-2 text-right">
                      {manage && (
                        <div className="flex justify-end gap-1">
                          <Button onClick={() => setDialog({ kind: "secret", cred: c })} title="Replace secret">
                            <RefreshCw className="h-3 w-3" aria-hidden /> Secret
                          </Button>
                          <Button onClick={() => setDialog({ kind: "edit", cred: c })} aria-label={`Edit ${c.name}`}>
                            <Pencil className="h-3 w-3" aria-hidden />
                          </Button>
                          <Button variant="danger" onClick={() => void remove(c)} aria-label={`Delete ${c.name}`}>
                            <Trash2 className="h-3 w-3" aria-hidden />
                          </Button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {dialog?.kind === "create" && <CreateDialog onClose={() => setDialog(null)} onDone={done} />}
      {dialog?.kind === "secret" && <ReplaceSecretDialog cred={dialog.cred} onClose={() => setDialog(null)} onDone={done} />}
      {dialog?.kind === "edit" && <EditDialog cred={dialog.cred} onClose={() => setDialog(null)} onDone={done} />}
    </div>
  );
}

export default function CredentialsPage() {
  return (
    <RequirePermission permission="inventory.read">
      <CredentialsList />
    </RequirePermission>
  );
}
