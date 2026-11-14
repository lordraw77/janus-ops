"use client";

import {
  ArrowUp,
  Download,
  File,
  FileSymlink,
  Folder,
  FolderPlus,
  Pencil,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import { type DragEvent, type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { Alert, Button, Field, Modal, TextInput } from "@/components/ui";
import { api, message } from "@/lib/api";
import type { InventoryNode } from "@/lib/inventory";

type Entry = {
  name: string;
  kind: "file" | "dir" | "link" | "other";
  size: number | null;
  mode: string | null;
  mtime: string | null;
  target_kind: string | null;
};

type Listing = { path: string; entries: Entry[]; truncated: boolean };

type Transfer = { id: number; name: string; pct: number; error?: string; done?: boolean };

type Dialog =
  | { kind: "mkdir" }
  | { kind: "rename"; entry: Entry }
  | { kind: "delete"; entry: Entry };

export function join(dir: string, name: string): string {
  return dir === "/" ? `/${name}` : `${dir}/${name}`;
}

function parent(path: string): string {
  const i = path.lastIndexOf("/");
  return i <= 0 ? "/" : path.slice(0, i);
}

function human(size: number | null): string {
  if (size === null) return "";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let v = size;
  let u = 0;
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024;
    u++;
  }
  return `${u === 0 ? v : v.toFixed(1)} ${units[u]}`;
}

function isDir(e: Entry): boolean {
  return e.kind === "dir" || (e.kind === "link" && e.target_kind === "dir");
}

type DirResult = { listing?: Listing; code?: string; dir: string | null };

/** List `target` on `node` (null = the remote user's home directory). */
async function fetchDir(node: string, target: string | null): Promise<DirResult> {
  let dir = target;
  if (dir === null) {
    const home = await api<{ path: string }>(`/sftp/${node}/home`);
    if (!home.ok) return { code: home.code, dir: null };
    dir = home.data.path;
  }
  const r = await api<Listing>(`/sftp/${node}/list?path=${encodeURIComponent(dir)}`);
  return r.ok ? { listing: r.data, dir } : { code: r.code, dir };
}

/** PUT the raw file with XMLHttpRequest: fetch() cannot report upload progress. */
function putFile(
  url: string,
  file: globalThis.File,
  onProgress: (pct: number) => void,
): Promise<{ ok: boolean; code: string }> {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve({ ok: true, code: "" });
      let code = `http_${xhr.status}`;
      try {
        const detail = (JSON.parse(xhr.responseText) as { detail?: unknown }).detail;
        if (typeof detail === "string") code = detail;
      } catch {
        /* not JSON */
      }
      resolve({ ok: false, code });
    };
    xhr.onerror = () => resolve({ ok: false, code: "network_error" });
    xhr.send(file);
  });
}

/**
 * SFTP file browser on one node. Files stream through Janus-Ops with the node's vault
 * credential; every download, upload, rename and deletion is in the audit log.
 */
export function FileBrowser({ nodes, initialNode }: { nodes: InventoryNode[]; initialNode?: string | null }) {
  const [nodeId, setNodeId] = useState(initialNode ?? "");
  const [typed, setTyped] = useState("");
  const [listing, setListing] = useState<Listing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(Boolean(initialNode));
  const [overwrite, setOverwrite] = useState(false);
  const [transfers, setTransfers] = useState<Transfer[]>([]);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const transferId = useRef(0);
  const request = useRef(0);
  const base = `/sftp/${nodeId}`;

  /** Apply a listing result; results of older requests (fast clicks) are dropped. */
  const apply = useCallback((id: number, result: DirResult) => {
    if (id !== request.current) return;
    setLoading(false);
    if (result.listing) {
      setListing(result.listing);
      setTyped(result.listing.path);
      setError(null);
    } else {
      setError(message(result.code ?? "error"));
      if (result.dir !== null) setTyped(result.dir);
    }
  }, []);

  // The parent remounts this component (key) to open another node from outside
  useEffect(() => {
    if (!initialNode) return;
    const id = ++request.current;
    void fetchDir(initialNode, null).then((r) => apply(id, r));
  }, [initialNode, apply]);

  function list(node: string, target: string | null) {
    const id = ++request.current;
    setLoading(true);
    void fetchDir(node, target).then((r) => apply(id, r));
  }

  function go(target: string) {
    list(nodeId, target);
  }

  function selectNode(id: string) {
    setNodeId(id);
    setListing(null);
    setError(null);
    setTyped("");
    if (id) list(id, null);
  }

  const cwd = listing?.path ?? "/";
  const load = (p: string) => go(p);

  async function upload(files: FileList | globalThis.File[]) {
    for (const file of Array.from(files)) {
      const id = ++transferId.current;
      setTransfers((t) => [...t, { id, name: file.name, pct: 0 }]);
      const update = (patch: Partial<Transfer>) =>
        setTransfers((t) => t.map((x) => (x.id === id ? { ...x, ...patch } : x)));
      const params = new URLSearchParams({ path: join(cwd, file.name), overwrite: String(overwrite) });
      const r = await putFile(`/api/v1${base}/upload?${params}`, file, (pct) => update({ pct }));
      update(r.ok ? { pct: 100, done: true } : { error: message(r.code) });
    }
    load(cwd);
  }

  async function submitDialog(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!dialog) return;
    const name = String(new FormData(e.currentTarget).get("name") ?? "").trim();
    let r;
    if (dialog.kind === "mkdir") {
      if (!name || name.includes("/")) return setError(message("invalid_path"));
      r = await api(`${base}/mkdir`, { method: "POST", body: { path: join(cwd, name) } });
    } else if (dialog.kind === "rename") {
      if (!name || name.includes("/")) return setError(message("invalid_path"));
      r = await api(`${base}/rename`, {
        method: "POST",
        body: { source: join(cwd, dialog.entry.name), target: join(cwd, name) },
      });
    } else {
      r = await api(`${base}/entry?path=${encodeURIComponent(join(cwd, dialog.entry.name))}`, { method: "DELETE" });
    }
    setDialog(null);
    if (!r.ok) setError(message(r.code));
    load(cwd);
  }

  function onDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length && listing) void upload(e.dataTransfer.files);
  }

  const crumbs = cwd === "/" ? [] : cwd.slice(1).split("/");

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <select
          value={nodeId}
          onChange={(e) => selectNode(e.target.value)}
          aria-label="Node"
          className="rounded border border-line bg-canvas px-2 py-1.5 text-xs outline-none focus:border-primary"
        >
          <option value="">Pick a node…</option>
          {nodes.map((n) => (
            <option key={n.id} value={n.id}>
              {n.name} ({n.address})
            </option>
          ))}
        </select>
        {nodeId && (
          <>
            <Button onClick={() => go(parent(cwd))} disabled={cwd === "/"} aria-label="Parent directory">
              <ArrowUp className="h-3 w-3" aria-hidden />
            </Button>
            <Button onClick={() => load(cwd)} aria-label="Refresh" disabled={!listing}>
              <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} aria-hidden />
            </Button>
            <form
              className="min-w-64 flex-1"
              onSubmit={(e) => {
                e.preventDefault();
                if (typed.startsWith("/")) go(typed);
                else setError(message("invalid_path"));
              }}
            >
              <TextInput value={typed} onChange={(e) => setTyped(e.target.value)} aria-label="Path" className="font-mono text-xs" />
            </form>
            <Button onClick={() => setDialog({ kind: "mkdir" })} disabled={!listing}>
              <FolderPlus className="h-3 w-3" aria-hidden /> New folder
            </Button>
            <Button variant="primary" onClick={() => fileInput.current?.click()} disabled={!listing}>
              <Upload className="h-3 w-3" aria-hidden /> Upload
            </Button>
            <label className="flex items-center gap-1 text-fg-secondary">
              <input type="checkbox" checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)} />
              Overwrite
            </label>
            <input
              ref={fileInput}
              type="file"
              multiple
              hidden
              onChange={(e) => {
                if (e.target.files?.length) void upload(e.target.files);
                e.target.value = "";
              }}
            />
          </>
        )}
      </div>

      {nodeId && (
        <nav aria-label="Path" className="flex flex-wrap items-center gap-1 font-mono text-[11px] text-fg-secondary">
          <button type="button" className="hover:text-primary" onClick={() => go("/")}>
            /
          </button>
          {crumbs.map((c, i) => (
            <span key={i} className="flex items-center gap-1">
              <button type="button" className="hover:text-primary" onClick={() => go(`/${crumbs.slice(0, i + 1).join("/")}`)}>
                {c}
              </button>
              {i < crumbs.length - 1 && <span>/</span>}
            </span>
          ))}
        </nav>
      )}

      {error && <Alert>{error}</Alert>}
      {listing?.truncated && <Alert kind="info">Only the first 5000 entries are shown.</Alert>}

      {transfers.length > 0 && (
        <div className="space-y-1 rounded border border-line p-2 text-[11px]">
          {transfers.map((t) => (
            <div key={t.id} className="flex items-center gap-2">
              <span className="w-56 truncate font-mono">{t.name}</span>
              <div className="h-1.5 flex-1 overflow-hidden rounded bg-elevated">
                <div className={`h-full ${t.error ? "bg-crit" : t.done ? "bg-ok" : "bg-primary"}`} style={{ width: `${t.pct}%` }} />
              </div>
              <span className={`w-72 truncate ${t.error ? "text-crit" : "text-fg-muted"}`}>
                {t.error ?? (t.done ? "uploaded" : `${t.pct}%`)}
              </span>
            </div>
          ))}
          <button type="button" className="text-fg-muted hover:text-fg" onClick={() => setTransfers((t) => t.filter((x) => !x.done && !x.error))}>
            Clear finished
          </button>
        </div>
      )}

      {!nodeId && (
        <p className="text-sm text-fg-muted">
          Pick a node with the SSH capability to browse its files. Transfers go through Janus-Ops
          with the node&apos;s vault credential and are recorded in the audit log.
        </p>
      )}

      {listing && (
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={`overflow-x-auto rounded border ${dragging ? "border-primary bg-primary/5" : "border-line"}`}
        >
          <table className="w-full text-left text-xs">
            <thead className="border-b border-line text-fg-muted">
              <tr>
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="py-2 pr-3 text-right font-medium">Size</th>
                <th className="py-2 pr-3 font-medium">Mode</th>
                <th className="py-2 pr-3 font-medium">Modified</th>
                <th />
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {listing.entries.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-3 text-fg-muted">
                    Empty directory. Drop files here to upload them.
                  </td>
                </tr>
              )}
              {listing.entries.map((e) => {
                const Icon = e.kind === "link" ? FileSymlink : isDir(e) ? Folder : File;
                const href = `/api/v1${base}/download?path=${encodeURIComponent(join(cwd, e.name))}`;
                return (
                  <tr key={e.name} className="hover:bg-elevated/50">
                    <td className="px-3 py-1.5">
                      {isDir(e) ? (
                        <button type="button" onClick={() => go(join(cwd, e.name))} className="flex items-center gap-2 text-left hover:text-primary">
                          <Icon className="h-3.5 w-3.5 text-telemetry" aria-hidden /> {e.name}
                        </button>
                      ) : (
                        <span className="flex items-center gap-2">
                          <Icon className="h-3.5 w-3.5 text-fg-muted" aria-hidden /> {e.name}
                        </span>
                      )}
                    </td>
                    <td className="py-1.5 pr-3 text-right font-mono text-fg-secondary">{e.kind === "file" ? human(e.size) : ""}</td>
                    <td className="py-1.5 pr-3 font-mono text-fg-secondary">{e.mode}</td>
                    <td className="whitespace-nowrap py-1.5 pr-3 text-fg-secondary">
                      {e.mtime ? new Date(e.mtime).toLocaleString() : ""}
                    </td>
                    <td className="py-1.5 pr-2">
                      <div className="flex justify-end gap-1">
                        {(e.kind === "file" || (e.kind === "link" && e.target_kind === "file")) && (
                          <a
                            href={href}
                            download={e.name}
                            aria-label={`Download ${e.name}`}
                            className="inline-flex items-center rounded border border-line bg-elevated px-2 py-1.5 hover:border-line-strong"
                          >
                            <Download className="h-3 w-3" aria-hidden />
                          </a>
                        )}
                        <Button onClick={() => setDialog({ kind: "rename", entry: e })} aria-label={`Rename ${e.name}`}>
                          <Pencil className="h-3 w-3" aria-hidden />
                        </Button>
                        <Button variant="danger" onClick={() => setDialog({ kind: "delete", entry: e })} aria-label={`Delete ${e.name}`}>
                          <Trash2 className="h-3 w-3" aria-hidden />
                        </Button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {dialog && (
        <Modal
          title={
            dialog.kind === "mkdir"
              ? "New folder"
              : dialog.kind === "rename"
                ? `Rename ${dialog.entry.name}`
                : `Delete ${dialog.entry.name}?`
          }
          onClose={() => setDialog(null)}
        >
          <form onSubmit={(e) => void submitDialog(e)} className="space-y-4">
            {dialog.kind === "delete" ? (
              <p className="text-sm text-fg-secondary">
                {isDir(dialog.entry) && dialog.entry.kind === "dir"
                  ? "Only empty directories can be deleted."
                  : "The file is deleted on the server. This cannot be undone."}
              </p>
            ) : (
              <Field label="Name" hint={`In ${cwd}`}>
                <TextInput name="name" autoFocus defaultValue={dialog.kind === "rename" ? dialog.entry.name : ""} />
              </Field>
            )}
            <div className="flex justify-end gap-2">
              <Button onClick={() => setDialog(null)}>Cancel</Button>
              <Button type="submit" variant={dialog.kind === "delete" ? "danger" : "primary"}>
                {dialog.kind === "delete" ? "Delete" : dialog.kind === "rename" ? "Rename" : "Create"}
              </Button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
