import { useCallback, useEffect, useState } from "react";

import { api, message } from "@/lib/api";

/** Playbook catalog API types (backend: janus_ops.ansible). */

export type SourceKind = "git" | "upload" | "editor" | "network";

export type Source = {
  id: string;
  name: string;
  kind: SourceKind;
  url: string | null;
  ref: string | null;
  mount_path: string | null;
  credential_id: string | null;
  writable: boolean;
  files: number;
  playbooks: number;
  created_by: string | null;
  created_at: string;
  last_synced_at: string | null;
  last_sync_status: "ok" | "error" | null;
  last_sync_error: string | null;
};

export type CatalogFile = {
  id: string;
  path: string;
  version: number;
  sha256: string;
  size: number;
  present: boolean;
  playbook: boolean;
  updated_at: string;
};

export type FileVersion = {
  version: number;
  sha256: string;
  size: number;
  origin: string;
  created_by: string | null;
  created_at: string;
};

export type FileDetail = CatalogFile & {
  source_id: string;
  source_name: string;
  source_kind: SourceKind;
  writable: boolean;
  content: string;
  versions: FileVersion[];
};

export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "error";

export type Run = {
  id: string;
  status: RunStatus;
  source_name: string;
  path: string;
  file_version: number;
  sha256: string;
  check_mode: boolean;
  node_names: Record<string, string>;
  skipped: { node: string; reason: string }[];
  started_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  return_code: number | null;
  error: string | null;
  log?: string | null;
};

export type SyncResult = {
  files: number;
  changed: number;
  skipped: { path: string; reason: string }[];
};

export const ACTIVE: RunStatus[] = ["queued", "running"];

/** Loads `path` (null = nothing to load). Keeps the last data while reloading the same path. */
export function useLoad<T>(path: string | null): {
  data: T | null;
  error: string | null;
  reload: () => void;
} {
  const [version, setVersion] = useState(0);
  const [state, setState] = useState<{ path: string | null; data: T | null; error: string | null }>(
    { path: null, data: null, error: null },
  );

  useEffect(() => {
    if (path === null) return;
    let cancelled = false;
    void api<T>(path).then((r) => {
      if (cancelled) return;
      setState({ path, data: r.ok ? r.data : null, error: r.ok ? null : message(r.code) });
    });
    return () => {
      cancelled = true;
    };
  }, [path, version]);

  const reload = useCallback(() => setVersion((v) => v + 1), []);
  if (path === null) return { data: null, error: null, reload };
  return {
    data: state.path === path ? state.data : null,
    error: state.path === path ? state.error : null,
    reload,
  };
}

/** Raw upload of a playbook file or archive (the body is not JSON). */
export async function uploadArchive(
  sourceId: string,
  file: File,
): Promise<{ ok: true; result: SyncResult } | { ok: false; error: string }> {
  try {
    const res = await fetch(
      `/api/v1/ansible/sources/${sourceId}/upload?filename=${encodeURIComponent(file.name)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
        cache: "no-store",
      },
    );
    const json: unknown = await res.json().catch(() => null);
    if (res.ok) return { ok: true, result: json as SyncResult };
    const detail = (json as { detail?: unknown } | null)?.detail;
    return { ok: false, error: message(typeof detail === "string" ? detail : `http_${res.status}`) };
  } catch {
    return { ok: false, error: message("network_error") };
  }
}

export function shortHash(sha: string): string {
  return sha.slice(0, 10);
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
