"use client";

import { type FormEvent, useState } from "react";

import { Alert, Button, Field, Modal, Select, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";

type Stack = {
  id: string;
  name: string;
  node_id: string;
  node_name: string;
  repo_url: string;
  branch: string;
  compose_file: string;
  credential_id: string | null;
  enabled: boolean;
  last_run_at: string | null;
  last_status: string | null;
};

type Run = {
  id: string;
  trigger: string;
  status: string;
  commit_sha: string | null;
  exit_code: number | null;
  reason: string | null;
  requested_by: string;
  started_at: string;
  finished_at: string | null;
};

type RunDetail = Run & { output: string };
type SnmpNode = { id: string; name: string };

const STATUS_COLOR: Record<string, string> = {
  ok: "text-ok",
  failed: "text-crit",
  refused: "text-warn",
  running: "text-primary",
};

/** The webhook secret is shown once: after creating or rotating a stack. */
function SecretDialog({ secret, onClose }: { secret: string; onClose: () => void }) {
  return (
    <Modal title="Webhook secret" onClose={onClose}>
      <div className="space-y-3 text-xs">
        <p className="text-fg-muted">
          Use this secret in the push webhook of your Git server (content type application/json, signed with
          HMAC-SHA256 as X-Hub-Signature-256). It is shown only now.
        </p>
        <pre className="overflow-x-auto rounded border border-line bg-canvas p-2 font-mono text-[11px]">{secret}</pre>
        <div className="flex justify-end">
          <Button type="button" variant="primary" onClick={onClose}>
            Done
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function CreateDialog({ onClose, onDone }: { onClose: () => void; onDone: (secret: string) => void }) {
  const nodes = useResource<SnmpNode[]>("/nodes");
  const credentials = useResource<{ id: string; name: string; kind: string }[]>("/credentials");
  const [credentialId, setCredentialId] = useState("");
  const [name, setName] = useState("");
  const [nodeId, setNodeId] = useState("");
  const [repo, setRepo] = useState("https://");
  const [branch, setBranch] = useState("main");
  const [composeFile, setComposeFile] = useState("docker-compose.yml");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const r = await api<{ webhook_secret: string }>("/gitops/stacks", {
      method: "POST",
      body: { name: name.trim(), node_id: nodeId, repo_url: repo.trim(), branch: branch.trim(), compose_file: composeFile.trim(), credential_id: credentialId || null },
    });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    onDone(r.data.webhook_secret);
  }

  return (
    <Modal title="New GitOps stack" onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        {error && <Alert>{error}</Alert>}
        <Field label="Name" hint="Lowercase letters, digits, - and _; it is the Compose project name">
          <TextInput autoFocus required value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Node" hint="The node must have the Janus agent with exec allowed">
          <Select required value={nodeId} onChange={(e) => setNodeId(e.target.value)}>
            <option value="">Choose a node</option>
            {(nodes.data ?? []).map((n) => (
              <option key={n.id} value={n.id}>
                {n.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Repository (https)" hint="No credentials in the URL">
          <TextInput required value={repo} onChange={(e) => setRepo(e.target.value)} />
        </Field>
        <Field label="Access token" hint="For a private repository: a credential of kind “Git access token”. The token is sent as a header, never written to the checkout.">
          <Select value={credentialId} onChange={(e) => setCredentialId(e.target.value)}>
            <option value="">None (public repository)</option>
            {(credentials.data ?? [])
              .filter((c) => c.kind === "git_token")
              .map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
          </Select>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Branch">
            <TextInput required value={branch} onChange={(e) => setBranch(e.target.value)} />
          </Field>
          <Field label="Compose file">
            <TextInput required value={composeFile} onChange={(e) => setComposeFile(e.target.value)} />
          </Field>
        </div>
        <div className="flex justify-end">
          <Button type="submit" variant="primary" disabled={busy || !name.trim() || !nodeId}>
            Create
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function RunPanel({ runId }: { runId: string }) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<string | null>(null);

  // loads the run once per id; the list refreshes the status
  if (loaded !== runId) {
    setLoaded(runId);
    void api<RunDetail>(`/gitops/runs/${runId}`).then((r) => {
      if (r.ok) setDetail(r.data);
      else setError(message(r.code));
    });
  }

  if (error) return <Alert>{error}</Alert>;
  if (!detail) return <p className="text-xs text-fg-muted">Loading the run…</p>;
  return (
    <div className="space-y-2 rounded border border-line p-3 text-xs">
      <div className="flex flex-wrap gap-3">
        <span className={`font-semibold ${STATUS_COLOR[detail.status] ?? ""}`}>{detail.status}</span>
        <span className="text-fg-muted">
          {detail.trigger} · by {detail.requested_by}
        </span>
        {detail.commit_sha && <span className="font-mono">commit {detail.commit_sha.slice(0, 12)}</span>}
      </div>
      {detail.reason && <div className="text-crit">{detail.reason}</div>}
      <pre className="max-h-[40vh] overflow-auto rounded border border-line bg-canvas p-2 font-mono text-[11px]">
        {detail.output || "(no output)"}
      </pre>
    </div>
  );
}

/** Stacks deployed from a Git repository on a node (FR-81). */
export function GitOpsView() {
  const { can } = useAuth();
  const stacks = useResource<Stack[]>("/gitops/stacks");
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const runs = useResource<Run[]>(selected ? `/gitops/stacks/${selected}/runs` : null);
  const stack = (stacks.data ?? []).find((s) => s.id === selected) ?? null;

  async function act(path: string, method: string, body?: unknown): Promise<unknown> {
    setError(null);
    const r = await api(path, { method, body });
    if (!r.ok) {
      setError(message(r.code));
      return null;
    }
    return r.data ?? true;
  }

  async function deploy() {
    if (!stack) return;
    const r = await act(`/gitops/stacks/${stack.id}/deploy`, "POST");
    if (r) {
      const run = (r as { run_id: string }).run_id;
      setRunId(run);
      runs.reload();
    }
  }

  async function rotate() {
    if (!stack) return;
    const r = await act(`/gitops/stacks/${stack.id}/rotate-secret`, "POST");
    if (r) setSecret((r as { webhook_secret: string }).webhook_secret);
  }

  async function toggle() {
    if (!stack) return;
    if (await act(`/gitops/stacks/${stack.id}`, "PATCH", { enabled: !stack.enabled })) stacks.reload();
  }

  async function remove() {
    if (!stack) return;
    if (await act(`/gitops/stacks/${stack.id}`, "DELETE")) {
      setSelected(null);
      stacks.reload();
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">GitOps</h1>
        {can("gitops.manage") && (
          <Button type="button" variant="primary" onClick={() => setCreating(true)}>
            New stack
          </Button>
        )}
      </div>
      {error && <Alert>{error}</Alert>}
      {stacks.error && <Alert>{stacks.error}</Alert>}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,20rem)_1fr]">
        <div className="space-y-1">
          {(stacks.data ?? []).length === 0 && <p className="text-xs text-fg-muted">No stacks yet.</p>}
          {(stacks.data ?? []).map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => {
                setSelected(s.id);
                setRunId(null);
              }}
              className={`block w-full rounded border p-2 text-left text-xs ${selected === s.id ? "border-primary" : "border-line"}`}
            >
              <div className="font-semibold">{s.name}</div>
              <div className="text-fg-muted">
                {s.node_name} · {s.branch}
                {!s.enabled && " · disabled"}
                {s.last_status && <span className={` · ${STATUS_COLOR[s.last_status] ?? ""}`}> {s.last_status}</span>}
              </div>
            </button>
          ))}
        </div>
        <div className="space-y-4">
          {!stack && <p className="text-xs text-fg-muted">Choose a stack to see its deploys.</p>}
          {stack && (
            <>
              <div className="space-y-1 text-xs">
                <div className="text-sm font-semibold">{stack.name}</div>
                <div className="font-mono text-fg-muted">{stack.repo_url}</div>
                {stack.credential_id && <div className="text-fg-muted">private repository (access token)</div>}
                <div className="text-fg-muted">
                  branch {stack.branch} · file {stack.compose_file} · node {stack.node_name}
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                {can("gitops.deploy") && (
                  <Button type="button" variant="primary" disabled={!stack.enabled} onClick={() => void deploy()}>
                    Deploy now
                  </Button>
                )}
                {can("gitops.manage") && (
                  <>
                    <Button type="button" onClick={() => void toggle()}>
                      {stack.enabled ? "Disable" : "Enable"}
                    </Button>
                    <Button type="button" onClick={() => void rotate()}>
                      New webhook secret
                    </Button>
                    <button type="button" onClick={() => void remove()} className="text-xs text-crit hover:underline">
                      delete
                    </button>
                  </>
                )}
              </div>
              <div className="text-xs font-semibold">Deploys</div>
              <div className="space-y-1">
                {(runs.data ?? []).length === 0 && <p className="text-xs text-fg-muted">No deploys yet.</p>}
                {(runs.data ?? []).map((r) => (
                  <button
                    key={r.id}
                    type="button"
                    onClick={() => setRunId(r.id)}
                    className={`flex w-full items-center gap-3 rounded border px-2 py-1 text-left text-xs ${runId === r.id ? "border-primary" : "border-line"}`}
                  >
                    <span className={`w-20 font-semibold ${STATUS_COLOR[r.status] ?? ""}`}>{r.status}</span>
                    <span className="text-fg-muted">{r.trigger}</span>
                    {r.commit_sha && <span className="font-mono text-fg-muted">{r.commit_sha.slice(0, 8)}</span>}
                    <span className="ml-auto text-fg-muted">{new Date(r.started_at).toLocaleString()}</span>
                  </button>
                ))}
              </div>
              {runId && <RunPanel key={runId} runId={runId} />}
            </>
          )}
        </div>
      </div>
      {creating && (
        <CreateDialog
          onClose={() => setCreating(false)}
          onDone={(s) => {
            setCreating(false);
            setSecret(s);
            stacks.reload();
          }}
        />
      )}
      {secret && <SecretDialog secret={secret} onClose={() => setSecret(null)} />}
    </div>
  );
}
