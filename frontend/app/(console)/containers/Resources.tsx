"use client";

import {
  CloudDownload,
  Eraser,
  Layers,
  Play,
  Plus,
  Save,
  RefreshCw,
  RotateCw,
  Square,
  Trash2,
} from "lucide-react";
import { useState } from "react";

import { SortTh } from "@/components/SortTh";
import { Alert, Button, Modal } from "@/components/ui";
import { formatBytes } from "@/lib/agents";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { type Getters, useSortedRows } from "@/lib/sort";

type Notice = { kind: "ok" | "error"; text: string } | null;

/** Runs one change, shows its outcome, reloads. Shared by every section. */
function useAction(reload: () => void) {
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState<string | null>(null);
  async function run(
    key: string,
    confirmText: string | null,
    request: () => ReturnType<typeof api>,
    done: (data: unknown) => string,
  ) {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(key);
    const r = await request();
    setBusy(null);
    setNotice(
      r.ok
        ? { kind: "ok", text: done(r.data) }
        : { kind: "error", text: message(r.code) },
    );
    reload();
  }
  return { notice, busy, run };
}

function Section({
  count,
  extra,
  onReload,
  notice,
  error,
  loading,
  children,
}: {
  count?: string;
  extra?: React.ReactNode;
  onReload: () => void;
  notice: Notice;
  error: string | null;
  /** shown while the first answer has not arrived (some calls take seconds) */
  loading?: string | false;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {count && (
          <span className="rounded-full bg-elevated px-2 py-0.5 font-mono text-xs">
            {count}
          </span>
        )}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {extra}
          <Button onClick={onReload}>
            <RefreshCw className="h-3 w-3" aria-hidden /> Refresh
          </Button>
        </div>
      </div>
      {notice && <Alert kind={notice.kind}>{notice.text}</Alert>}
      {error && <Alert>{error}</Alert>}
      {loading && !error && (
        <p className="py-6 text-center text-xs text-fg-muted">{loading}</p>
      )}
      {children}
    </div>
  );
}

const TH = "px-2 py-2 font-medium";
const TD = "px-2 py-2";
const Table = ({ children }: { children: React.ReactNode }) => (
  <div className="overflow-x-auto rounded-lg border border-line bg-surface">
    <table className="w-full text-xs">{children}</table>
  </div>
);
const Empty = ({ cols, text }: { cols: number; text: string }) => (
  <tr>
    <td colSpan={cols} className="px-2 py-6 text-center text-fg-muted">
      {text}
    </td>
  </tr>
);
const Pill = ({
  children,
  warn,
}: {
  children: React.ReactNode;
  warn?: boolean;
}) => (
  <span
    className={`rounded-full px-2 py-0.5 text-[10px] ${warn ? "bg-warn/20 text-warn" : "bg-elevated text-fg-secondary"}`}
  >
    {children}
  </span>
);

// ------------------------------------------------------------------ Stacks

type StackRow = {
  name: string;
  working_dir: string | null;
  config_files: string | null;
  services: string[];
  containers: number;
  running: number;
};
type StackDetail = {
  name: string;
  working_dir: string | null;
  config_files: string | null;
  version: string | null;
  containers: {
    id: string;
    name: string;
    service: string | null;
    image: string;
    state: string;
    status: string;
  }[];
  services: string[];
  images: string[];
  networks: { id: string; name: string; driver: string }[];
  volumes: { name: string; driver: string }[];
};
const STACK_GET: Getters<StackRow> = {
  name: (s) => s.name,
  services: (s) => s.services.length,
  state: (s) => s.running / (s.containers || 1),
};

type Compose = {
  path: string;
  files: string[];
  working_dir: string;
  content: string;
};

function ComposeEditor({ nodeId, name }: { nodeId: string; name: string }) {
  const { can } = useAuth();
  const manage = can("containers.manage");
  const res = useResource<Compose>(
    `/containers/${nodeId}/stacks/${encodeURIComponent(name)}/compose`,
  );
  const [text, setText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const value = text ?? res.data?.content ?? "";
  const dirty = text !== null && text !== res.data?.content;
  async function save(apply: boolean) {
    if (
      apply &&
      !window.confirm(`Save and run "docker compose up -d" for ${name}?`)
    )
      return;
    setBusy(true);
    setNotice(null);
    const r = await api<{ output: string }>(
      `/containers/${nodeId}/stacks/${encodeURIComponent(name)}/compose?apply=${apply}`,
      { method: "PUT", body: { content: value } },
    );
    setBusy(false);
    if (r.ok) {
      setNotice({
        kind: "ok",
        text: apply
          ? `Saved and applied.${r.data.output ? ` ${r.data.output.trim().slice(-300)}` : ""}`
          : "Saved (the previous file is kept as .bak). Apply it to recreate the containers.",
      });
      res.reload();
      setText(null);
    } else setNotice({ kind: "error", text: message(r.code) });
  }
  return (
    <div className="space-y-2 text-xs">
      {res.error && <Alert>{res.error}</Alert>}
      {!res.data && !res.error && (
        <p className="py-4 text-center text-fg-muted">
          Reading the file from the host…
        </p>
      )}
      {res.data && (
        <>
          <p className="break-all font-mono text-fg-muted">
            {res.data.path}
            {res.data.files.length > 1 &&
              ` (+${res.data.files.length - 1} more file(s), not editable here)`}
          </p>
          <textarea
            value={value}
            onChange={(e) => setText(e.target.value)}
            readOnly={!manage}
            spellCheck={false}
            aria-label="Compose file"
            className="h-80 w-full rounded border border-line bg-canvas p-2 font-mono text-xs outline-none focus:border-primary"
          />
          {notice && <Alert kind={notice.kind}>{notice.text}</Alert>}
          {manage && (
            <div className="flex gap-2">
              <Button
                onClick={() => void save(false)}
                disabled={busy || !dirty}
              >
                <Save className="h-3 w-3" aria-hidden /> Validate and save
              </Button>
              <Button onClick={() => void save(true)} disabled={busy || !dirty}>
                <CloudDownload className="h-3 w-3" aria-hidden /> Save and apply
              </Button>
              {busy && (
                <span className="self-center text-fg-muted">Working…</span>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function StackDialog({
  nodeId,
  name,
  onClose,
}: {
  nodeId: string;
  name: string;
  onClose: () => void;
}) {
  const d = useResource<StackDetail>(
    `/containers/${nodeId}/stacks/${encodeURIComponent(name)}`,
  );
  const [tab, setTab] = useState<"overview" | "compose">("overview");
  const x = d.data;
  return (
    <Modal title={`Stack ${name}`} onClose={onClose} wide>
      <div className="mb-3 flex gap-1" role="tablist">
        {(["overview", "compose"] as const).map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={`rounded px-3 py-1 text-xs font-semibold capitalize ${tab === t ? "bg-elevated text-fg" : "text-fg-muted hover:text-fg"}`}
          >
            {t}
          </button>
        ))}
      </div>
      {tab === "compose" && <ComposeEditor nodeId={nodeId} name={name} />}
      {d.error && tab === "overview" && <Alert>{d.error}</Alert>}
      {x && tab === "overview" && (
        <div className="space-y-4 text-xs">
          <dl className="grid grid-cols-[8rem_1fr] gap-y-1">
            <dt className="text-fg-muted">Directory</dt>
            <dd className="font-mono">{x.working_dir ?? "—"}</dd>
            <dt className="text-fg-muted">Compose file</dt>
            <dd className="break-all font-mono">{x.config_files ?? "—"}</dd>
            <dt className="text-fg-muted">Compose version</dt>
            <dd className="font-mono">{x.version ?? "—"}</dd>
          </dl>
          <div>
            <h3 className="mb-1 font-semibold">Containers</h3>
            <Table>
              <tbody>
                {x.containers.map((c) => (
                  <tr
                    key={c.id}
                    className="border-t border-line first:border-0"
                  >
                    <td className={`${TD} font-semibold`}>
                      {c.service ?? "?"}
                    </td>
                    <td className={`${TD} font-mono`}>{c.name}</td>
                    <td className={`${TD} font-mono text-fg-muted`}>
                      {c.image}
                    </td>
                    <td className={TD}>
                      <Pill warn={c.state !== "running"}>{c.status}</Pill>
                    </td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <h3 className="mb-1 font-semibold">Networks</h3>
              {x.networks.length === 0 && (
                <p className="text-fg-muted">None.</p>
              )}
              {x.networks.map((n) => (
                <div key={n.id} className="font-mono">
                  {n.name} <span className="text-fg-muted">({n.driver})</span>
                </div>
              ))}
            </div>
            <div>
              <h3 className="mb-1 font-semibold">Volumes</h3>
              {x.volumes.length === 0 && <p className="text-fg-muted">None.</p>}
              {x.volumes.map((v) => (
                <div key={v.name} className="font-mono">
                  {v.name}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </Modal>
  );
}

const NEW_STACK = `services:
  web:
    image: nginx:alpine
    ports:
      - "8080:80"
    restart: unless-stopped
`;

function NewStackDialog({
  nodeId,
  onClose,
  onDone,
}: {
  nodeId: string;
  onClose: () => void;
  onDone: (name: string) => void;
}) {
  const [name, setName] = useState("");
  const [content, setContent] = useState(NEW_STACK);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function create() {
    setBusy(true);
    setError(null);
    const r = await api(`/containers/${nodeId}/stacks`, {
      method: "POST",
      body: { name, content },
    });
    setBusy(false);
    if (r.ok) onDone(name);
    else setError(message(r.code));
  }
  return (
    <Modal title="New stack" onClose={onClose} wide>
      <div className="space-y-3 text-xs">
        <label className="block">
          <span className="text-fg-muted">
            Name (lowercase letters, digits, - and _)
          </span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value.toLowerCase())}
            className="mt-1 block w-64 rounded border border-line bg-canvas px-2 py-1.5 font-mono outline-none focus:border-primary"
          />
        </label>
        <textarea
          value={content}
          onChange={(e) => setContent(e.target.value)}
          spellCheck={false}
          aria-label="Compose file"
          className="h-72 w-full rounded border border-line bg-canvas p-2 font-mono outline-none focus:border-primary"
        />
        <p className="text-fg-muted">
          The file is saved on the host under{" "}
          <code>/var/lib/janus-ops/stacks/{name || "name"}</code> and started
          with <code>docker compose up -d</code> (images are pulled, it can take
          a while).
        </p>
        {error && <Alert>{error}</Alert>}
        <div className="flex gap-2">
          <Button
            variant="primary"
            onClick={() => void create()}
            disabled={busy || !name || !content.trim()}
          >
            <Play className="h-3 w-3" aria-hidden />{" "}
            {busy ? "Deploying…" : "Deploy"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export function StacksSection({ nodeId }: { nodeId: string }) {
  const { can } = useAuth();
  const manage = can("containers.manage");
  const res = useResource<StackRow[]>(`/containers/${nodeId}/stacks`);
  const { notice, busy, run } = useAction(res.reload);
  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const { sorted, sort, toggle } = useSortedRows(res.data ?? [], STACK_GET, {
    col: "name",
    dir: "asc",
  });
  const base = `/containers/${nodeId}/stacks`;
  const act = (s: StackRow, a: "start" | "stop" | "restart" | "update") =>
    run(
      s.name,
      a === "stop" || a === "update"
        ? a === "stop"
          ? `Stop every container of ${s.name}?`
          : `Pull images and recreate every container of ${s.name}?`
        : null,
      () =>
        api(`${base}/${encodeURIComponent(s.name)}/${a}`, { method: "POST" }),
      () => `${s.name}: ${a} done.`,
    );
  const down = (s: StackRow, volumes: boolean) =>
    run(
      s.name,
      volumes
        ? `Remove ${s.name}: containers, networks AND its volumes (data is lost)?`
        : `Remove the containers and networks of ${s.name}? Volumes are kept.`,
      () =>
        api(`${base}/${encodeURIComponent(s.name)}/down?volumes=${volumes}`, {
          method: "POST",
        }),
      () => `${s.name}: removed.`,
    );
  return (
    <Section
      count={`${sorted.length} stacks`}
      onReload={res.reload}
      notice={notice}
      error={res.error}
      loading={!res.data && "Loading…"}
      extra={
        manage && (
          <Button variant="primary" onClick={() => setCreating(true)}>
            <Plus className="h-3 w-3" aria-hidden /> New stack
          </Button>
        )
      }
    >
      {res.data && (
        <Table>
          <thead className="bg-elevated text-left text-fg-secondary">
            <tr>
              <SortTh
                label="Stack"
                col="name"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <SortTh
                label="Services"
                col="services"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <SortTh
                label="Running"
                col="state"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <th className={TH}>Compose file</th>
              <th className={TH} />
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && (
              <Empty cols={5} text="No Compose projects on this host." />
            )}
            {sorted.map((s) => (
              <tr key={s.name} className="border-t border-line">
                <td className={TD}>
                  <button
                    onClick={() => setOpen(s.name)}
                    className="flex items-center gap-1.5 font-semibold text-primary hover:underline"
                  >
                    <Layers className="h-3.5 w-3.5" aria-hidden /> {s.name}
                  </button>
                </td>
                <td className={`${TD} font-mono`}>{s.services.join(", ")}</td>
                <td className={TD}>
                  <Pill warn={s.running < s.containers}>
                    {s.running}/{s.containers}
                  </Pill>
                </td>
                <td className={`${TD} break-all font-mono text-fg-muted`}>
                  {s.config_files ?? s.working_dir ?? "—"}
                </td>
                <td className={`${TD} text-right`}>
                  {manage && (
                    <span className="flex justify-end gap-1">
                      <Button
                        onClick={() => void act(s, "start")}
                        disabled={busy === s.name}
                        title="Start"
                      >
                        <Play className="h-3 w-3" aria-hidden />
                      </Button>
                      <Button
                        onClick={() => void act(s, "restart")}
                        disabled={busy === s.name}
                        title="Restart"
                      >
                        <RotateCw className="h-3 w-3" aria-hidden />
                      </Button>
                      <Button
                        onClick={() => void act(s, "stop")}
                        disabled={busy === s.name}
                        title="Stop"
                      >
                        <Square className="h-3 w-3" aria-hidden />
                      </Button>
                      <Button
                        onClick={() => void act(s, "update")}
                        disabled={busy === s.name}
                        title="Pull and recreate"
                      >
                        <CloudDownload className="h-3 w-3" aria-hidden />
                      </Button>
                      <Button
                        onClick={() => void down(s, false)}
                        disabled={busy === s.name}
                        title="Remove containers and networks (keep volumes)"
                      >
                        <Trash2 className="h-3 w-3" aria-hidden /> down
                      </Button>
                      <Button
                        onClick={() => void down(s, true)}
                        disabled={busy === s.name}
                        title="Remove containers, networks and volumes"
                      >
                        <Trash2 className="h-3 w-3" aria-hidden /> down +
                        volumes
                      </Button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {open && (
        <StackDialog
          nodeId={nodeId}
          name={open}
          onClose={() => setOpen(null)}
        />
      )}
      {creating && (
        <NewStackDialog
          nodeId={nodeId}
          onClose={() => setCreating(false)}
          onDone={(n) => {
            setCreating(false);
            res.reload();
            setOpen(n);
          }}
        />
      )}
    </Section>
  );
}

// ------------------------------------------------------------------ Images

type ImageRow = {
  id: string;
  tags: string[];
  size: number;
  created: number;
  containers: number;
  dangling: boolean;
};
const IMAGE_GET: Getters<ImageRow> = {
  name: (i) => i.tags[0] ?? i.id,
  size: (i) => i.size,
  created: (i) => i.created,
  used: (i) => i.containers,
};

export function ImagesSection({ nodeId }: { nodeId: string }) {
  const { can } = useAuth();
  const manage = can("containers.manage");
  const res = useResource<ImageRow[]>(`/containers/${nodeId}/images`);
  const { notice, busy, run } = useAction(res.reload);
  const [pull, setPull] = useState("");
  const { sorted, sort, toggle } = useSortedRows(res.data ?? [], IMAGE_GET, {
    col: "created",
    dir: "desc",
  });
  const base = `/containers/${nodeId}/images`;
  const remove = (ref: string, force = false) =>
    run(
      ref,
      `Remove the image ${ref}?`,
      () =>
        api(`${base}?ref=${encodeURIComponent(ref)}&force=${force}`, {
          method: "DELETE",
        }),
      () => `${ref}: removed.`,
    );
  const prune = (unused: boolean) =>
    run(
      "*",
      unused
        ? "Remove every image no container uses?"
        : "Remove untagged (dangling) images?",
      () => api(`${base}/prune?unused=${unused}`, { method: "POST" }),
      (d) => {
        const r = d as { removed: number; reclaimed: number };
        return `${r.removed} image(s) removed, ${formatBytes(r.reclaimed)} freed.`;
      },
    );
  return (
    <Section
      count={`${sorted.length} images`}
      onReload={res.reload}
      notice={notice}
      error={res.error}
      loading={!res.data && "Loading…"}
      extra={
        manage && (
          <>
            <input
              value={pull}
              onChange={(e) => setPull(e.target.value)}
              placeholder="nginx:latest"
              aria-label="Image to pull"
              className="w-48 rounded border border-line bg-canvas px-2 py-1.5 text-xs outline-none focus:border-primary"
            />
            <Button
              disabled={!pull.trim() || busy === "pull"}
              onClick={() =>
                void run(
                  "pull",
                  null,
                  () =>
                    api(`${base}/pull?ref=${encodeURIComponent(pull.trim())}`, {
                      method: "POST",
                    }),
                  () => `${pull.trim()}: pulled.`,
                ).then(() => setPull(""))
              }
            >
              <CloudDownload className="h-3 w-3" aria-hidden /> Pull
            </Button>
            <Button onClick={() => void prune(false)} disabled={busy === "*"}>
              <Eraser className="h-3 w-3" aria-hidden /> Prune dangling
            </Button>
            <Button onClick={() => void prune(true)} disabled={busy === "*"}>
              <Eraser className="h-3 w-3" aria-hidden /> Prune unused
            </Button>
          </>
        )
      }
    >
      {res.data && (
        <Table>
          <thead className="bg-elevated text-left text-fg-secondary">
            <tr>
              <SortTh
                label="Image"
                col="name"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <th className={TH}>ID</th>
              <SortTh
                label="Size"
                col="size"
                sort={sort}
                onSort={toggle}
                className={`${TH} text-right`}
              />
              <SortTh
                label="Created"
                col="created"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <SortTh
                label="Used by"
                col="used"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <th className={TH} />
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && <Empty cols={6} text="No images." />}
            {sorted.map((i) => (
              <tr key={i.id} className="border-t border-line align-top">
                <td className={`${TD} font-mono`}>
                  {i.dangling ? (
                    <Pill warn>dangling</Pill>
                  ) : (
                    i.tags.map((t) => <div key={t}>{t}</div>)
                  )}
                </td>
                <td className={`${TD} font-mono text-fg-muted`}>{i.id}</td>
                <td className={`${TD} text-right font-mono tabular-nums`}>
                  {formatBytes(i.size)}
                </td>
                <td className={`${TD} text-fg-secondary`}>
                  {new Date(i.created * 1000).toLocaleDateString()}
                </td>
                <td className={TD}>
                  {i.containers > 0 ? (
                    <Pill>{i.containers} container(s)</Pill>
                  ) : (
                    <span className="text-fg-muted">unused</span>
                  )}
                </td>
                <td className={`${TD} text-right`}>
                  {manage && (
                    <Button
                      onClick={() => void remove(i.dangling ? i.id : i.tags[0])}
                      disabled={busy === (i.dangling ? i.id : i.tags[0])}
                      title="Remove"
                    >
                      <Trash2 className="h-3 w-3" aria-hidden />
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------ Volumes

type VolumeRow = {
  name: string;
  driver: string;
  mountpoint: string;
  created: string;
  size: number | null;
  used_by: number | null;
  project: string | null;
};
const VOLUME_GET: Getters<VolumeRow> = {
  name: (v) => v.name,
  size: (v) => v.size,
  used: (v) => v.used_by,
  stack: (v) => v.project,
};

export function VolumesSection({ nodeId }: { nodeId: string }) {
  const { can } = useAuth();
  const manage = can("containers.manage");
  const [sizes, setSizes] = useState(false);
  // sizes make the daemon walk every volume (seconds on a big host): only on request
  const res = useResource<VolumeRow[]>(
    `/containers/${nodeId}/volumes${sizes ? "?sizes=true" : ""}`,
  );
  const { notice, busy, run } = useAction(res.reload);
  const { sorted, sort, toggle } = useSortedRows(res.data ?? [], VOLUME_GET, {
    col: "name",
    dir: "asc",
  });
  const base = `/containers/${nodeId}/volumes`;
  const unused = (res.data ?? []).filter((v) => v.used_by === 0).length;
  return (
    <Section
      count={`${sorted.length} volumes`}
      onReload={res.reload}
      notice={notice}
      error={res.error}
      loading={
        !res.data &&
        (sizes ? "Measuring volumes, this can take a while…" : "Loading…")
      }
      extra={
        <>
          {!sizes && <Button onClick={() => setSizes(true)}>Show sizes</Button>}
          {manage && (
            <Button
              disabled={unused === 0 || busy === "*"}
              onClick={() =>
                void run(
                  "*",
                  `Delete ${unused} unused volume(s)? Their data is lost for good.`,
                  () => api(`${base}/prune`, { method: "POST" }),
                  (d) => {
                    const r = d as { removed: number; reclaimed: number };
                    return `${r.removed} volume(s) removed, ${formatBytes(r.reclaimed)} freed.`;
                  },
                )
              }
            >
              <Eraser className="h-3 w-3" aria-hidden /> Prune unused ({unused})
            </Button>
          )}
        </>
      }
    >
      {res.data && (
        <Table>
          <thead className="bg-elevated text-left text-fg-secondary">
            <tr>
              <SortTh
                label="Name"
                col="name"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <SortTh
                label="Stack"
                col="stack"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <th className={TH}>Driver</th>
              <SortTh
                label="Size"
                col="size"
                sort={sort}
                onSort={toggle}
                className={`${TH} text-right`}
              />
              <SortTh
                label="Used by"
                col="used"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <th className={TH}>Mountpoint</th>
              <th className={TH} />
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && <Empty cols={7} text="No volumes." />}
            {sorted.map((v) => (
              <tr key={v.name} className="border-t border-line">
                <td className={`${TD} break-all font-mono`}>{v.name}</td>
                <td className={TD}>
                  {v.project ?? <span className="text-fg-muted">—</span>}
                </td>
                <td className={`${TD} font-mono text-fg-muted`}>{v.driver}</td>
                <td className={`${TD} text-right font-mono tabular-nums`}>
                  {v.size === null ? "—" : formatBytes(v.size)}
                </td>
                <td className={TD}>
                  {v.used_by === 0 ? (
                    <Pill warn>unused</Pill>
                  ) : v.used_by === null ? (
                    "—"
                  ) : (
                    <Pill>{v.used_by} container(s)</Pill>
                  )}
                </td>
                <td className={`${TD} break-all font-mono text-fg-muted`}>
                  {v.mountpoint}
                </td>
                <td className={`${TD} text-right`}>
                  {manage && (
                    <Button
                      onClick={() =>
                        void run(
                          v.name,
                          `Delete the volume ${v.name}? Its data is lost for good.`,
                          () =>
                            api(`${base}/${encodeURIComponent(v.name)}`, {
                              method: "DELETE",
                            }),
                          () => `${v.name}: removed.`,
                        )
                      }
                      disabled={busy === v.name}
                      title="Delete"
                    >
                      <Trash2 className="h-3 w-3" aria-hidden />
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------ Networks

type NetworkRow = {
  id: string;
  name: string;
  driver: string;
  scope: string;
  internal: boolean;
  subnets: string[];
  gateways: string[];
  containers: string[];
  project: string | null;
  builtin: boolean;
};
const NETWORK_GET: Getters<NetworkRow> = {
  name: (n) => n.name,
  driver: (n) => n.driver,
  stack: (n) => n.project,
  used: (n) => n.containers.length,
};

export function NetworksSection({ nodeId }: { nodeId: string }) {
  const { can } = useAuth();
  const manage = can("containers.manage");
  const res = useResource<NetworkRow[]>(`/containers/${nodeId}/networks`);
  const { notice, busy, run } = useAction(res.reload);
  const { sorted, sort, toggle } = useSortedRows(res.data ?? [], NETWORK_GET, {
    col: "name",
    dir: "asc",
  });
  const base = `/containers/${nodeId}/networks`;
  return (
    <Section
      count={`${sorted.length} networks`}
      onReload={res.reload}
      notice={notice}
      error={res.error}
      loading={!res.data && "Loading…"}
      extra={
        manage && (
          <Button
            disabled={busy === "*"}
            onClick={() =>
              void run(
                "*",
                "Remove every network no container uses?",
                () => api(`${base}/prune`, { method: "POST" }),
                (d) =>
                  `${(d as { removed: number }).removed} network(s) removed.`,
              )
            }
          >
            <Eraser className="h-3 w-3" aria-hidden /> Prune unused
          </Button>
        )
      }
    >
      {res.data && (
        <Table>
          <thead className="bg-elevated text-left text-fg-secondary">
            <tr>
              <SortTh
                label="Name"
                col="name"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <SortTh
                label="Driver"
                col="driver"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <th className={TH}>Subnet</th>
              <th className={TH}>Gateway</th>
              <SortTh
                label="Stack"
                col="stack"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <SortTh
                label="Containers"
                col="used"
                sort={sort}
                onSort={toggle}
                className={TH}
              />
              <th className={TH} />
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && <Empty cols={7} text="No networks." />}
            {sorted.map((n) => (
              <tr key={n.id} className="border-t border-line align-top">
                <td className={`${TD} font-mono`}>
                  {n.name} {n.internal && <Pill>internal</Pill>}
                </td>
                <td className={`${TD} font-mono text-fg-muted`}>
                  {n.driver}/{n.scope}
                </td>
                <td className={`${TD} font-mono`}>
                  {n.subnets.join(", ") || "—"}
                </td>
                <td className={`${TD} font-mono`}>
                  {n.gateways.join(", ") || "—"}
                </td>
                <td className={TD}>
                  {n.project ?? <span className="text-fg-muted">—</span>}
                </td>
                <td className={`${TD} font-mono`}>
                  {n.containers.length === 0 ? (
                    <span className="text-fg-muted">none</span>
                  ) : (
                    n.containers.map((c) => <div key={c}>{c}</div>)
                  )}
                </td>
                <td className={`${TD} text-right`}>
                  {manage && !n.builtin && (
                    <Button
                      onClick={() =>
                        void run(
                          n.id,
                          `Remove the network ${n.name}?`,
                          () => api(`${base}/${n.id}`, { method: "DELETE" }),
                          () => `${n.name}: removed.`,
                        )
                      }
                      disabled={busy === n.id}
                      title="Remove"
                    >
                      <Trash2 className="h-3 w-3" aria-hidden />
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------ Disk

type Usage = {
  count: number;
  size: number;
  reclaimable: number;
  stopped?: number;
};
type Disk = {
  images: Usage;
  containers: Usage;
  volumes: Usage;
  build_cache: Usage;
};

export function DiskSection({ nodeId }: { nodeId: string }) {
  const { can } = useAuth();
  const manage = can("containers.manage");
  const res = useResource<Disk>(`/containers/${nodeId}/disk`);
  const { notice, busy, run } = useAction(res.reload);
  const base = `/containers/${nodeId}`;
  const rows: [string, keyof Disk][] = [
    ["Images", "images"],
    ["Containers (writable layer)", "containers"],
    ["Volumes", "volumes"],
    ["Build cache", "build_cache"],
  ];
  const prune = (what: "containers" | "build-cache", text: string) =>
    run(
      what,
      text,
      () => api(`${base}/prune/${what}`, { method: "POST" }),
      (d) => `${formatBytes((d as { reclaimed: number }).reclaimed)} freed.`,
    );
  return (
    <Section
      onReload={res.reload}
      notice={notice}
      error={res.error}
      loading={
        !res.data && "Measuring disk usage, this can take several seconds…"
      }
    >
      {res.data && (
        <Table>
          <thead className="bg-elevated text-left text-fg-secondary">
            <tr>
              <th className={TH}>Type</th>
              <th className={`${TH} text-right`}>Count</th>
              <th className={`${TH} text-right`}>Size</th>
              <th className={`${TH} text-right`}>Reclaimable</th>
              <th className={TH} />
            </tr>
          </thead>
          <tbody>
            {rows.map(([label, key]) => {
              const u = res.data![key];
              return (
                <tr key={key} className="border-t border-line">
                  <td className={TD}>{label}</td>
                  <td className={`${TD} text-right font-mono`}>{u.count}</td>
                  <td className={`${TD} text-right font-mono`}>
                    {formatBytes(u.size)}
                  </td>
                  <td className={`${TD} text-right font-mono`}>
                    {formatBytes(u.reclaimable)}
                  </td>
                  <td className={`${TD} text-right`}>
                    {manage && key === "containers" && (
                      <Button
                        disabled={!u.stopped || busy === "containers"}
                        onClick={() =>
                          void prune(
                            "containers",
                            `Remove ${u.stopped} stopped container(s)?`,
                          )
                        }
                      >
                        <Eraser className="h-3 w-3" aria-hidden /> Remove
                        stopped ({u.stopped ?? 0})
                      </Button>
                    )}
                    {manage && key === "build_cache" && (
                      <Button
                        disabled={busy === "build-cache"}
                        onClick={() =>
                          void prune(
                            "build-cache",
                            "Clear the unused build cache?",
                          )
                        }
                      >
                        <Eraser className="h-3 w-3" aria-hidden /> Clear
                      </Button>
                    )}
                    {key === "images" && (
                      <span className="text-fg-muted">see Images</span>
                    )}
                    {key === "volumes" && (
                      <span className="text-fg-muted">see Volumes</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}
    </Section>
  );
}
