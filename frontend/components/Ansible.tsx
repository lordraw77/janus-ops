"use client";

import { Play, Plus, RefreshCw, Save, Trash2, Upload } from "lucide-react";
import { type ChangeEvent, type FormEvent, useEffect, useRef, useState } from "react";

import { Alert, Button, Card, Field, Modal, Select, TextInput } from "@/components/ui";
import {
  ACTIVE,
  type CatalogFile,
  type FileDetail,
  type Run,
  type RunStatus,
  type Source,
  type SourceKind,
  type SurveyQuestion,
  type SyncResult,
  formatSize,
  shortHash,
  uploadArchive,
  useLoad,
} from "@/lib/ansible";
import { api, message } from "@/lib/api";
import { useAuth } from "@/lib/auth";

type Notice = { kind: "ok" | "error" | "info"; text: string };

type NodeRow = {
  id: string;
  name: string;
  environment: string;
  address: string;
  capabilities: { kind: string; enabled: boolean }[];
};

type CredentialRow = { id: string; name: string; kind: string };

type VersionView = { version: number; content: string; origin: string; created_at: string };

const DEFAULT_PLAYBOOK = "---\n- hosts: all\n  gather_facts: false\n  tasks: []\n";

const RUN_STYLE: Record<RunStatus, string> = {
  queued: "border-telemetry/40 text-telemetry",
  running: "border-telemetry/40 text-telemetry",
  succeeded: "border-ok/40 text-ok",
  failed: "border-crit/40 text-crit",
  error: "border-crit/40 text-crit",
};

function summarise(result: SyncResult): string {
  const skipped = result.skipped.length ? `, ${result.skipped.length} skipped` : "";
  return `${result.files} file${result.files === 1 ? "" : "s"}, ${result.changed} changed${skipped}`;
}

function skippedText(skipped: { node?: string; path?: string; reason: string }[]): string {
  return skipped
    .map((s) => `${s.node ?? s.path ?? ""}: ${message(s.reason)}`)
    .join(" ");
}

export function AnsibleConsole() {
  const { can } = useAuth();
  const manage = can("ansible.manage");
  const runAllowed = can("ansible.run");

  const sources = useLoad<Source[]>("/ansible/sources");
  const [pickedSource, setPickedSource] = useState<string | null>(null);
  const [pickedFile, setPickedFile] = useState<string | null>(null);
  const [viewVersion, setViewVersion] = useState<number | null>(null);
  const [pickedRun, setPickedRun] = useState<string | null>(null);
  const [modal, setModal] = useState<"source" | "file" | "run" | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const uploadInput = useRef<HTMLInputElement>(null);

  const sourceId = pickedSource ?? sources.data?.[0]?.id ?? null;
  const source = sources.data?.find((s) => s.id === sourceId) ?? null;
  const files = useLoad<CatalogFile[]>(sourceId ? `/ansible/sources/${sourceId}/files` : null);
  const fileId = files.data?.some((f) => f.id === pickedFile) ? pickedFile : null;
  const detail = useLoad<FileDetail>(fileId ? `/ansible/files/${fileId}` : null);
  const version = useLoad<VersionView>(
    fileId && viewVersion !== null ? `/ansible/files/${fileId}/versions/${viewVersion}` : null,
  );

  const runs = useLoad<Run[]>("/ansible/runs?limit=30");
  const hasActive = (runs.data ?? []).some((r) => ACTIVE.includes(r.status));
  const runDetail = useLoad<Run>(pickedRun ? `/ansible/runs/${pickedRun}` : null);
  const runIsActive = runDetail.data !== null && ACTIVE.includes(runDetail.data.status);

  // Poll while something is running; the server keeps the log, the page only asks again.
  usePoll(hasActive, runs.reload);
  usePoll(runIsActive, runDetail.reload);

  function pickSource(id: string) {
    setPickedSource(id);
    setPickedFile(null);
    setViewVersion(null);
  }

  function pickFile(id: string) {
    setPickedFile(id);
    setViewVersion(null);
  }

  async function run(action: () => Promise<{ ok: boolean; text: string; kind?: Notice["kind"] }>) {
    setBusy(true);
    const result = await action();
    setBusy(false);
    setNotice({ kind: result.kind ?? (result.ok ? "ok" : "error"), text: result.text });
  }

  async function syncSource(s: Source) {
    await run(async () => {
      const r = await api<SyncResult>(`/ansible/sources/${s.id}/sync`, { method: "POST" });
      files.reload();
      sources.reload();
      return r.ok
        ? { ok: true, text: `Synced ${s.name}: ${summarise(r.data)}.${r.data.skipped.length ? " " + skippedText(r.data.skipped) : ""}` }
        : { ok: false, text: `Sync failed: ${message(r.code)}` };
    });
  }

  async function deleteSource(s: Source) {
    if (!window.confirm(`Delete the source "${s.name}" and its catalog? Past runs keep their record.`)) return;
    await run(async () => {
      const r = await api(`/ansible/sources/${s.id}`, { method: "DELETE" });
      if (!r.ok) return { ok: false, text: message(r.code) };
      setPickedSource(null);
      setPickedFile(null);
      sources.reload();
      return { ok: true, text: `Deleted ${s.name}.` };
    });
  }

  async function onUpload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || !source) return;
    await run(async () => {
      const r = await uploadArchive(source.id, file);
      if (!r.ok) return { ok: false, text: r.error };
      files.reload();
      sources.reload();
      return { ok: true, text: `Uploaded ${file.name}: ${summarise(r.result)}.${r.result.skipped.length ? " " + skippedText(r.result.skipped) : ""}` };
    });
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-fg">Ansible playbooks</h1>
          <p className="text-xs text-fg-muted">
            Playbooks are stored as versions in the catalog. A run uses the versions it started with.
          </p>
        </div>
        <Button onClick={() => sources.reload()} aria-label="Reload sources">
          <RefreshCw size={14} />
        </Button>
      </div>

      {notice && <Alert kind={notice.kind === "ok" ? "ok" : notice.kind === "info" ? "info" : "error"}>{notice.text}</Alert>}
      {sources.error && <Alert>{sources.error}</Alert>}

      <div className="grid gap-4 lg:grid-cols-[300px_1fr]">
        <Card
          title="Sources"
          actions={
            manage && (
              <Button onClick={() => setModal("source")}>
                <Plus size={14} /> New
              </Button>
            )
          }
        >
          {sources.data?.length === 0 && <p className="text-xs text-fg-muted">No sources yet.</p>}
          <ul className="divide-y divide-line">
            {sources.data?.map((s) => (
              <li key={s.id}>
                <button
                  type="button"
                  onClick={() => pickSource(s.id)}
                  className={`w-full py-2 text-left text-sm ${s.id === sourceId ? "text-primary" : "text-fg"}`}
                >
                  <span className="font-medium">{s.name}</span>
                  <span className="ml-2 text-[11px] uppercase text-fg-muted">{s.kind}</span>
                  <span className="block text-[11px] text-fg-muted">
                    {s.files} files · {s.playbooks} playbooks
                    {s.last_sync_status === "error" && <span className="text-crit"> · sync failed: {message(s.last_sync_error ?? "")}</span>}
                  </span>
                </button>
              </li>
            ))}
          </ul>

          {source && (
            <div className="mt-3 space-y-2 border-t border-line pt-3">
              <p className="text-[11px] text-fg-muted">
                {source.kind === "git" && `Git: ${source.url}${source.ref ? ` @ ${source.ref}` : ""}`}
                {source.kind === "network" && `Mount: ${source.mount_path}`}
                {source.kind === "sftp" && `SFTP: ${source.url}`}
                {(source.kind === "editor" || source.kind === "upload") && "Writable in Janus-Ops"}
              </p>
              <div className="flex flex-wrap gap-2">
                {manage && (source.kind === "git" || source.kind === "network" || source.kind === "sftp") && (
                  <Button disabled={busy} onClick={() => void syncSource(source)}>
                    <RefreshCw size={14} /> Sync
                  </Button>
                )}
                {manage && source.kind === "upload" && (
                  <>
                    <Button disabled={busy} onClick={() => uploadInput.current?.click()}>
                      <Upload size={14} /> Upload
                    </Button>
                    <input
                      ref={uploadInput}
                      type="file"
                      accept=".yml,.yaml,.zip,.tar.gz,.tgz"
                      className="hidden"
                      onChange={(e) => void onUpload(e)}
                    />
                  </>
                )}
                {manage && (source.kind === "editor" || source.kind === "upload") && (
                  <Button onClick={() => setModal("file")}>
                    <Plus size={14} /> New file
                  </Button>
                )}
                {manage && (
                  <Button variant="danger" disabled={busy} onClick={() => void deleteSource(source)}>
                    <Trash2 size={14} /> Delete
                  </Button>
                )}
              </div>
            </div>
          )}

          {files.data && files.data.length > 0 && (
            <ul className="mt-3 divide-y divide-line border-t border-line pt-2 font-mono text-xs">
              {files.data.map((f) => (
                <li key={f.id}>
                  <button
                    type="button"
                    onClick={() => pickFile(f.id)}
                    className={`flex w-full items-center justify-between gap-2 py-1.5 text-left ${f.id === fileId ? "text-primary" : f.present ? "text-fg" : "text-fg-muted line-through"}`}
                  >
                    <span className="truncate" title={f.path}>{f.path}</span>
                    <span className="shrink-0 text-fg-muted">v{f.version}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title={detail.data ? detail.data.path : "Editor"}>
          {!detail.data && <p className="text-sm text-fg-muted">Pick a file to see and edit its content.</p>}
          {detail.data && (
            <FileEditor
              key={`${detail.data.id}:${detail.data.version}:${viewVersion ?? "current"}`}
              detail={detail.data}
              viewing={version.data}
              viewVersion={viewVersion}
              onView={setViewVersion}
              canEdit={manage && detail.data.writable}
              canRun={runAllowed && detail.data.playbook && detail.data.present}
              onRunRequested={() => setModal("run")}
              onChanged={(text) => {
                setNotice({ kind: "ok", text });
                detail.reload();
                files.reload();
                sources.reload();
              }}
              onError={(text) => setNotice({ kind: "error", text })}
            />
          )}
        </Card>
      </div>

      <Card
        title="Runs"
        actions={
          <Button onClick={() => runs.reload()} aria-label="Reload runs">
            <RefreshCw size={14} />
          </Button>
        }
      >
        {runs.error && <Alert>{runs.error}</Alert>}
        {runs.data?.length === 0 && <p className="text-xs text-fg-muted">No runs yet.</p>}
        {runs.data && runs.data.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-line text-fg-muted">
                <tr>
                  <th className="py-2 pr-3 font-medium">Started</th>
                  <th className="py-2 pr-3 font-medium">Status</th>
                  <th className="py-2 pr-3 font-medium">Playbook</th>
                  <th className="py-2 pr-3 font-medium">Nodes</th>
                  <th className="py-2 pr-3 font-medium">Mode</th>
                  <th className="py-2 font-medium">By</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {runs.data.map((r) => (
                  <tr
                    key={r.id}
                    onClick={() => setPickedRun(r.id)}
                    className={`cursor-pointer hover:bg-elevated ${r.id === pickedRun ? "bg-elevated" : ""}`}
                  >
                    <td className="whitespace-nowrap py-1.5 pr-3 text-fg-secondary">{new Date(r.created_at).toLocaleString()}</td>
                    <td className="py-1.5 pr-3">
                      <span className={`rounded border px-1.5 py-0.5 ${RUN_STYLE[r.status]}`}>{r.status}</span>
                    </td>
                    <td className="py-1.5 pr-3 font-mono">{r.source_name}:{r.path} <span className="text-fg-muted">v{r.file_version}</span></td>
                    <td className="py-1.5 pr-3">{Object.values(r.node_names).join(", ") || (r.error ? message(r.error) : "—")}</td>
                    <td className="py-1.5 pr-3">{r.check_mode ? "dry run" : "apply"}</td>
                    <td className="py-1.5">{r.started_by}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {runDetail.data && (
          <div className="mt-4 space-y-2 border-t border-line pt-3">
            <p className="text-xs text-fg-muted">
              Run {runDetail.data.id.slice(0, 8)} · {runDetail.data.status}
              {runDetail.data.return_code !== null && ` · exit ${runDetail.data.return_code}`}
              {runDetail.data.error && ` · ${message(runDetail.data.error)}`}
              {" · hash "}
              <span className="font-mono">{shortHash(runDetail.data.sha256)}</span>
            </p>
            {runDetail.data.skipped.length > 0 && (
              <Alert kind="info">Skipped: {skippedText(runDetail.data.skipped)}</Alert>
            )}
            <pre className="max-h-96 overflow-auto rounded border border-line bg-canvas p-3 font-mono text-[11px] text-fg-secondary">
              {runDetail.data.log || (ACTIVE.includes(runDetail.data.status) ? "Waiting for output…" : "No output.")}
            </pre>
          </div>
        )}
      </Card>

      {modal === "source" && manage && (
        <SourceModal
          onClose={() => setModal(null)}
          onCreated={(id, name) => {
            setModal(null);
            setPickedSource(id);
            setPickedFile(null);
            sources.reload();
            setNotice({ kind: "ok", text: `Created source ${name}.` });
          }}
          onError={(text) => setNotice({ kind: "error", text })}
        />
      )}
      {modal === "file" && source && manage && (
        <NewFileModal
          sourceId={source.id}
          onClose={() => setModal(null)}
          onCreated={(id) => {
            setModal(null);
            setPickedFile(id);
            setViewVersion(null);
            files.reload();
          }}
        />
      )}
      {modal === "run" && detail.data && runAllowed && (
        <RunModal
          fileId={detail.data.id}
          path={detail.data.path}
          canReadNodes={can("inventory.read")}
          onClose={() => setModal(null)}
          onStarted={(id, status, skipped) => {
            setModal(null);
            setPickedRun(id);
            runs.reload();
            setNotice(
              status === "error"
                ? { kind: "error", text: `No node can run this playbook. ${skippedText(skipped)}` }
                : {
                    kind: "ok",
                    text: skipped.length ? `Run started. Skipped: ${skippedText(skipped)}` : "Run started.",
                  },
            );
          }}
          onError={(text) => setNotice({ kind: "error", text })}
        />
      )}
    </div>
  );
}

/** Calls `reload` every 2.5 s while `active`. */
function usePoll(active: boolean, reload: () => void) {
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(reload, 2500);
    return () => window.clearInterval(timer);
  }, [active, reload]);
}

function FileEditor({
  detail,
  viewing,
  viewVersion,
  onView,
  canEdit,
  canRun,
  onRunRequested,
  onChanged,
  onError,
}: {
  detail: FileDetail;
  viewing: VersionView | null;
  viewVersion: number | null;
  onView: (v: number | null) => void;
  canEdit: boolean;
  canRun: boolean;
  onRunRequested: () => void;
  onChanged: (text: string) => void;
  onError: (text: string) => void;
}) {
  const [draft, setDraft] = useState(detail.content);
  const [compareWith, setCompareWith] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const readOnly = !canEdit || viewVersion !== null;
  const diff = useLoad<{ diff: string }>(
    compareWith !== null ? `/ansible/files/${detail.id}/diff?from=${compareWith}&to=${detail.version}` : null,
  );

  async function save() {
    setSaving(true);
    const r = await api<{ version: number; changed: boolean }>(`/ansible/files/${detail.id}`, {
      method: "PUT",
      body: { content: draft, expected_version: detail.version },
    });
    setSaving(false);
    if (!r.ok) return onError(message(r.code));
    if (!r.data.changed) return onError("Nothing changed: the content is the same as the current version.");
    onChanged(`Saved ${detail.path} as version ${r.data.version}.`);
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
        <span>
          {detail.source_name} · {detail.source_kind} · {formatSize(detail.size)} · sha {shortHash(detail.sha256)}
        </span>
        {!detail.present && <span className="text-crit">removed from the source</span>}
        <Field label="Version">
          <Select
            value={viewVersion ?? detail.version}
            onChange={(e) => {
              const v = Number(e.target.value);
              onView(v === detail.version ? null : v);
            }}
          >
            {detail.versions.map((v) => (
              <option key={v.version} value={v.version}>
                v{v.version} · {v.origin} · {new Date(v.created_at).toLocaleString()}
              </option>
            ))}
          </Select>
        </Field>
        {viewVersion !== null && (
          <Button onClick={() => onView(null)}>Back to current</Button>
        )}
      </div>

      <textarea
        aria-label="File content"
        spellCheck={false}
        readOnly={readOnly}
        value={viewing ? viewing.content : draft}
        onChange={(e) => setDraft(e.target.value)}
        rows={22}
        className="w-full rounded border border-line bg-canvas p-3 font-mono text-xs text-fg outline-none focus:border-primary read-only:opacity-80"
      />

      <div className="flex flex-wrap items-center gap-2">
        {canEdit && viewVersion === null && (
          <>
            <Button variant="primary" disabled={saving || draft === detail.content} onClick={() => void save()}>
              <Save size={14} /> Save version
            </Button>
            <Button disabled={draft === detail.content} onClick={() => setDraft(detail.content)}>
              Discard changes
            </Button>
          </>
        )}
        {canRun && viewVersion === null && (
          <Button disabled={draft !== detail.content} onClick={onRunRequested}>
            <Play size={14} /> Run…
          </Button>
        )}
        {detail.versions.length > 1 && (
          <Field label="Compare with">
            <Select
              value={compareWith ?? ""}
              onChange={(e) => setCompareWith(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">—</option>
              {detail.versions
                .filter((v) => v.version !== detail.version)
                .map((v) => (
                  <option key={v.version} value={v.version}>
                    v{v.version}
                  </option>
                ))}
            </Select>
          </Field>
        )}
      </div>
      {!canEdit && <p className="text-[11px] text-fg-muted">Read-only: changes need ansible.manage on an editable source.</p>}
      {canEdit && viewVersion === null && draft !== detail.content && (
        <p className="text-[11px] text-fg-muted">Unsaved changes. Run is available after saving.</p>
      )}
      {compareWith !== null && diff.data && (
        <pre className="max-h-96 overflow-auto rounded border border-line bg-canvas p-3 font-mono text-[11px] text-fg-secondary">
          {diff.data.diff || "No differences."}
        </pre>
      )}
    </div>
  );
}

function SourceModal({
  onClose,
  onCreated,
  onError,
}: {
  onClose: () => void;
  onCreated: (id: string, name: string) => void;
  onError: (text: string) => void;
}) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<SourceKind>("editor");
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [credentialId, setCredentialId] = useState("");
  const [mountPath, setMountPath] = useState("");
  const [saving, setSaving] = useState(false);
  const credentials = useLoad<CredentialRow[]>(kind === "git" || kind === "sftp" ? "/credentials" : null);
  const keys = (credentials.data ?? []).filter((c) => c.kind === "ssh_key");
  const sftpCredentials = (credentials.data ?? []).filter((c) => c.kind === "ssh_key" || c.kind === "ssh_password");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    const body: Record<string, unknown> = { name: name.trim(), kind };
    if (kind === "git") {
      body.url = url.trim();
      body.ref = ref.trim() || null;
      body.credential_id = credentialId || null;
    }
    if (kind === "sftp") {
      body.url = url.trim();
      body.credential_id = credentialId || null;
    }
    if (kind === "network") body.mount_path = mountPath.trim();
    const r = await api<{ id: string }>("/ansible/sources", { method: "POST", body });
    setSaving(false);
    if (r.ok) onCreated(r.data.id, name.trim());
    else onError(message(r.code));
  }

  return (
    <Modal title="New playbook source" onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <Field label="Name">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} />
        </Field>
        <Field label="Kind">
          <Select value={kind} onChange={(e) => setKind(e.target.value as SourceKind)}>
            <option value="editor">Editor (written in Janus-Ops)</option>
            <option value="upload">Upload (file or archive)</option>
            <option value="git">Git repository (read-only)</option>
            <option value="network">Mounted directory on the server (read-only)</option>
            <option value="sftp">Directory on an SFTP server (read-only)</option>
          </Select>
        </Field>
        {kind === "git" && (
          <>
            <Field label="Repository URL" hint="https://, ssh:// or user@host:path. No credentials in the URL.">
              <TextInput value={url} onChange={(e) => setUrl(e.target.value)} required />
            </Field>
            <Field label="Branch or tag (optional)">
              <TextInput value={ref} onChange={(e) => setRef(e.target.value)} />
            </Field>
            <Field label="Deploy key (SSH key credential, optional)" hint={credentials.error ?? undefined}>
              <Select value={credentialId} onChange={(e) => setCredentialId(e.target.value)}>
                <option value="">None (public repository)</option>
                {keys.map((k) => (
                  <option key={k.id} value={k.id}>
                    {k.name}
                  </option>
                ))}
              </Select>
            </Field>
          </>
        )}
        {kind === "sftp" && (
          <>
            <Field
              label="SFTP URL"
              hint="sftp://user@host[:port]/absolute/directory. The host key is pinned on the first sync."
            >
              <TextInput
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="sftp://deploy@files.example.lan/srv/ansible"
                required
              />
            </Field>
            <Field label="Credential (SSH key or password)" hint={credentials.error ?? undefined}>
              <Select value={credentialId} onChange={(e) => setCredentialId(e.target.value)} required>
                <option value="">Choose a credential</option>
                {sftpCredentials.map((k) => (
                  <option key={k.id} value={k.id}>
                    {k.name}
                  </option>
                ))}
              </Select>
            </Field>
          </>
        )}
        {kind === "network" && (
          <Field label="Directory on the Janus-Ops host" hint="Must be under a mount root allowed by the server.">
            <TextInput value={mountPath} onChange={(e) => setMountPath(e.target.value)} required />
          </Field>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit" disabled={saving}>
            Create
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function NewFileModal({
  sourceId,
  onClose,
  onCreated,
}: {
  sourceId: string;
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const [path, setPath] = useState("site.yml");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const r = await api<{ id: string }>(`/ansible/sources/${sourceId}/files`, {
      method: "POST",
      body: { path: path.trim(), content: DEFAULT_PLAYBOOK },
    });
    if (r.ok) onCreated(r.data.id);
    else setError(message(r.code));
  }

  return (
    <Modal title="New file" onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <Field label="Path inside the source" hint="Relative, e.g. site.yml or roles/web/tasks/main.yml">
          <TextInput value={path} onChange={(e) => setPath(e.target.value)} required />
        </Field>
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit">
            Create
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function RunModal({
  fileId,
  path,
  canReadNodes,
  onClose,
  onStarted,
  onError,
}: {
  fileId: string;
  path: string;
  canReadNodes: boolean;
  onClose: () => void;
  onStarted: (id: string, status: string, skipped: { node: string; reason: string }[]) => void;
  onError: (text: string) => void;
}) {
  const nodes = useLoad<NodeRow[]>(canReadNodes ? "/nodes" : null);
  const survey = useLoad<{ questions: SurveyQuestion[] }>(`/ansible/files/${fileId}/survey`);
  const questions = survey.data?.questions ?? [];
  const [picked, setPicked] = useState<string[]>([]);
  const [check, setCheck] = useState(true);
  const [answers, setAnswers] = useState<Record<string, string | boolean>>({});
  const [saving, setSaving] = useState(false);
  const sshNodes = (nodes.data ?? []).filter((n) => n.capabilities.some((c) => c.kind === "ssh" && c.enabled));

  function toggle(id: string) {
    setPicked((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    // Only the answers the user gave are sent: the server fills the defaults and checks the types
    const survey: Record<string, string | boolean> = {};
    for (const q of questions) {
      const value = answers[q.name];
      if (value === undefined || value === "") continue;
      survey[q.name] = typeof value === "boolean" ? value : String(value);
    }
    const r = await api<{ id: string; status: string; skipped: { node: string; reason: string }[] }>(
      `/ansible/files/${fileId}/runs`,
      { method: "POST", body: { node_ids: picked, check, survey } },
    );
    setSaving(false);
    if (r.ok) onStarted(r.data.id, r.data.status, r.data.skipped);
    else onError(message(r.code));
  }

  return (
    <Modal title={`Run ${path}`} onClose={onClose} wide>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <p className="text-xs text-fg-muted">
          The run uses the saved version of the file and the current versions of the other files in its source. Only nodes with
          SSH enabled, a pinned host key and a key credential can run.
        </p>
        {!canReadNodes && <Alert>You need inventory.read to pick nodes.</Alert>}
        {nodes.error && <Alert>{nodes.error}</Alert>}
        {questions.length > 0 && (
          <fieldset className="space-y-3 rounded border border-line p-3">
            <legend className="px-1 text-xs uppercase text-fg-muted">Survey</legend>
            {questions.map((q) => {
              const fallback = q.default ?? (q.type === "boolean" ? false : "");
              const value = answers[q.name] ?? fallback;
              const set = (next: string | boolean) => setAnswers((prev) => ({ ...prev, [q.name]: next }));
              if (q.type === "boolean") {
                return (
                  <label key={q.name} className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={value === true} onChange={(e) => set(e.target.checked)} />
                    {q.label}
                  </label>
                );
              }
              return (
                <Field key={q.name} label={`${q.label}${q.required ? " *" : ""}`} hint={q.type === "integer" ? "Whole number" : undefined}>
                  {q.type === "choice" ? (
                    <Select value={String(value)} onChange={(e) => set(e.target.value)}>
                      {(q.choices ?? []).map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </Select>
                  ) : (
                    <TextInput
                      value={String(value)}
                      onChange={(e) => set(e.target.value)}
                      required={q.required}
                      inputMode={q.type === "integer" ? "numeric" : undefined}
                    />
                  )}
                </Field>
              );
            })}
          </fieldset>
        )}
        {survey.error && <Alert>{survey.error}</Alert>}
        <div className="max-h-72 overflow-auto rounded border border-line">
          {sshNodes.length === 0 && <p className="p-3 text-xs text-fg-muted">No nodes with SSH.</p>}
          {sshNodes.map((n) => (
            <label key={n.id} className="flex cursor-pointer items-center gap-3 border-b border-line px-3 py-2 text-sm last:border-0">
              <input type="checkbox" checked={picked.includes(n.id)} onChange={() => toggle(n.id)} />
              <span className="font-medium">{n.name}</span>
              <span className="text-xs text-fg-muted">
                {n.address} · {n.environment}
              </span>
            </label>
          ))}
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={check} onChange={(e) => setCheck(e.target.checked)} />
          Dry run (ansible --check): report changes without applying them
        </label>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit" disabled={saving || picked.length === 0}>
            <Play size={14} /> {check ? "Dry run" : "Apply"} on {picked.length} node{picked.length === 1 ? "" : "s"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
