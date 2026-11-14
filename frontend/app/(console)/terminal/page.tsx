"use client";

import "@xterm/xterm/css/xterm.css";

import { Circle, FolderOpen, Play, Radio, RotateCcw, SquareTerminal, X } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { FileBrowser } from "@/components/FileBrowser";
import { type Snippet, SnippetLibrary } from "@/components/Snippets";
import {
  monoFont,
  TERMINAL_THEME,
  type TerminalHandle,
  type TerminalStatus,
  TerminalView,
} from "@/components/TerminalView";
import { Alert, Button, Card, Modal } from "@/components/ui";
import { api, message, type Resource, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { InventoryNode } from "@/lib/inventory";

type Tab = { key: string; nodeId: string; status: TerminalStatus };

type SessionRow = {
  id: string;
  username: string | null;
  node_name: string | null;
  target: string | null;
  remote_user: string | null;
  started_at: string;
  ended_at: string | null;
  end_reason: string | null;
  bytes_out: number;
  recorded: boolean;
};

type HostKey = { node_id: string; node_name: string; key_type: string; fingerprint: string; first_seen: string };

const REASONS: Record<string, string> = {
  remote_closed: "Session ended on the server",
  client_closed: "Closed",
  idle_timeout: "Closed after inactivity",
  session_ended: "Your Janus-Ops session ended",
  connection_lost: "Connection lost",
};

const CONNECT_ERRORS: Record<string, string> = {
  host_key_mismatch:
    "The server's host key changed. This can mean an attack, or a reinstall: if expected, an operator resets the pinned key in the Host keys tab.",
  auth_failed: "The server refused the credential.",
  unreachable: "The server is not reachable.",
  no_ssh_capability: "This node has no SSH capability.",
  ssh_disabled: "The SSH capability of this node is disabled.",
  no_credential: "No credential is attached to the SSH capability.",
  no_username: "No user name: set it on the credential or on the SSH capability.",
  nested_jump_not_supported: "Only one level of jump host is supported.",
  too_many_terminals: "Too many open terminals: close one first.",
  credential_unreadable: "The credential cannot be decrypted. Replace its secret.",
  vault_unavailable: "The vault is not configured on this server.",
  forbidden: "You do not have the ssh.connect permission.",
  not_authenticated: "Your session has expired. Sign in again.",
  cross_origin: "Connection refused (origin check).",
};

function connectError(code: string): string {
  if (code.startsWith("jump_")) return `Jump host: ${connectError(code.slice(5))}`;
  return CONNECT_ERRORS[code] ?? message(code);
}

function statusDot(s: TerminalStatus): string {
  if (s.state === "connected") return "text-ok";
  if (s.state === "connecting") return "text-warn";
  if (s.state === "error") return "text-crit";
  return "text-offline";
}

// ---------------------------------------------------------------- live terminals
function Terminals({
  nodes,
  snippets,
  onFiles,
}: {
  nodes: InventoryNode[];
  snippets: Snippet[];
  onFiles?: (nodeId: string) => void;
}) {
  const params = useSearchParams();
  const initial = params.get("view") === "files" ? null : params.get("node");
  const counter = useRef(1);
  const [tabs, setTabs] = useState<Tab[]>(() =>
    initial ? [{ key: "t0", nodeId: initial, status: { state: "connecting" } }] : [],
  );
  const [active, setActive] = useState<string | null>(initial ? "t0" : null);
  const [pick, setPick] = useState("");
  const names = useMemo(() => new Map(nodes.map((n) => [n.id, n.name])), [nodes]);
  const handles = useRef(new Map<string, TerminalHandle>());
  // Broadcast: keystrokes typed in a member terminal go to every member terminal
  const [broadcast, setBroadcast] = useState(false);
  const [members, setMembers] = useState<Set<string>>(new Set());
  const [snippetId, setSnippetId] = useState("");

  function toggleBroadcast() {
    if (!broadcast) setMembers(new Set(tabs.filter((t) => t.status.state === "connected").map((t) => t.key)));
    setBroadcast(!broadcast);
  }

  function toggleMember(key: string) {
    setMembers((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const route = (key: string) => (data: string) => {
    if (!broadcast || !members.has(key)) return false;
    for (const k of members) handles.current.get(k)?.input(data);
    return true;
  };

  function runSnippet() {
    if (!snippetId || !current) return;
    const targets = broadcast && members.has(current.key) ? [...members] : [current.key];
    for (const k of targets) handles.current.get(k)?.snippet(snippetId, targets.length > 1);
  }

  const setHandle = useCallback((key: string, handle: TerminalHandle | null) => {
    if (handle) handles.current.set(key, handle);
    else handles.current.delete(key);
  }, []);

  function open(nodeId: string) {
    const key = `t${counter.current++}`;
    setTabs((prev) => [...prev, { key, nodeId, status: { state: "connecting" } }]);
    setActive(key);
  }

  function close(key: string) {
    setMembers((prev) => {
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
    const rest = tabs.filter((t) => t.key !== key);
    setTabs(rest);
    if (active === key) setActive(rest.at(-1)?.key ?? null);
  }

  function reconnect(tab: Tab) {
    close(tab.key);
    open(tab.nodeId);
  }

  const setStatus = useCallback((key: string, status: TerminalStatus) => {
    setTabs((prev) => prev.map((t) => (t.key === key ? { ...t, status } : t)));
  }, []);

  const current = tabs.find((t) => t.key === active);

  return (
    <div className="flex h-[calc(100vh-11rem)] min-h-96 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1 border-b border-line pb-2 text-xs">
        {tabs.map((t) => (
          <div
            key={t.key}
            className={`flex items-center gap-1.5 rounded border px-2 py-1 ${broadcast && members.has(t.key) ? "border-warn/60" : t.key === active ? "border-line-strong" : "border-line"} ${t.key === active ? "bg-elevated" : ""}`}
          >
            {broadcast && (
              <input
                type="checkbox"
                checked={members.has(t.key)}
                onChange={() => toggleMember(t.key)}
                aria-label={`Broadcast to ${names.get(t.nodeId) ?? t.nodeId}`}
                className="accent-warn"
              />
            )}
            <button type="button" onClick={() => setActive(t.key)} className="flex items-center gap-1.5">
              <Circle className={`h-2 w-2 fill-current ${statusDot(t.status)}`} aria-hidden />
              {names.get(t.nodeId) ?? t.nodeId.slice(0, 8)}
            </button>
            <button type="button" onClick={() => close(t.key)} aria-label="Close terminal" className="text-fg-muted hover:text-fg">
              <X className="h-3 w-3" aria-hidden />
            </button>
          </div>
        ))}
        <div className="ml-auto flex items-center gap-1">
          <Button
            onClick={toggleBroadcast}
            disabled={tabs.length < 2 && !broadcast}
            className={broadcast ? "border-warn/60 text-warn" : ""}
            aria-pressed={broadcast}
            title="Type into several terminals at once"
          >
            <Radio className="h-3.5 w-3.5" aria-hidden /> Broadcast
          </Button>
          <select
            value={snippetId}
            onChange={(e) => setSnippetId(e.target.value)}
            aria-label="Snippet"
            className="max-w-48 rounded border border-line bg-canvas px-2 py-1 text-xs outline-none focus:border-primary"
          >
            <option value="">Snippet…</option>
            {snippets.map((s) => (
              <option key={s.id} value={s.id}>
                {s.shared ? "★ " : ""}
                {s.name}
              </option>
            ))}
          </select>
          <Button
            onClick={runSnippet}
            disabled={!snippetId || current?.status.state !== "connected"}
            title={broadcast ? "Run in all broadcast terminals" : "Run in the current terminal"}
          >
            <Play className="h-3.5 w-3.5" aria-hidden /> Run
          </Button>
          <span className="mx-1 h-4 border-l border-line" />
          <select
            value={pick}
            onChange={(e) => setPick(e.target.value)}
            aria-label="Node"
            className="rounded border border-line bg-canvas px-2 py-1 text-xs outline-none focus:border-primary"
          >
            <option value="">Connect to…</option>
            {nodes.map((n) => (
              <option key={n.id} value={n.id}>
                {n.name} ({n.address})
              </option>
            ))}
          </select>
          <Button variant="primary" disabled={!pick} onClick={() => pick && open(pick)}>
            <SquareTerminal className="h-3.5 w-3.5" aria-hidden /> Open
          </Button>
        </div>
      </div>

      {current && current.status.state !== "connected" && current.status.state !== "connecting" && (
        <div className="flex items-center justify-between gap-2">
          <Alert kind={current.status.state === "error" ? "error" : "info"}>
            {current.status.state === "error"
              ? connectError(current.status.code)
              : REASONS[current.status.reason] ?? current.status.reason}
          </Alert>
          <Button onClick={() => reconnect(current)}>
            <RotateCcw className="h-3 w-3" aria-hidden /> Reconnect
          </Button>
        </div>
      )}
      {broadcast && (
        <Alert kind="info">
          Broadcast on: keystrokes and snippets typed in a checked terminal go to all {members.size}{" "}
          checked terminals.
        </Alert>
      )}
      {current?.status.state === "connected" && (
        <div className="flex items-center gap-2 text-[11px] text-fg-muted">
          {current.status.user}@{names.get(current.nodeId)}
          {current.status.recorded && <span className="text-crit">● recorded</span>}
          {onFiles && (
            <button type="button" onClick={() => onFiles(current.nodeId)} className="ml-auto inline-flex items-center gap-1 hover:text-primary">
              <FolderOpen className="h-3 w-3" aria-hidden /> Files on this node
            </button>
          )}
        </div>
      )}

      <div className="relative flex-1">
        {tabs.length === 0 && (
          <p className="text-sm text-fg-muted">
            Pick a node with the SSH capability to open a terminal. Credentials come from the vault
            and never reach the browser.
          </p>
        )}
        {tabs.map((t) => (
          <div key={t.key} className={`absolute inset-0 ${t.key === active ? "" : "invisible"}`}>
            <TerminalView
              nodeId={t.nodeId}
              active={t.key === active}
              onStatus={(s) => setStatus(t.key, s)}
              onHandle={(h) => setHandle(t.key, h)}
              routeInput={route(t.key)}
            />
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- recordings
type CastEvent = [number, string, string];

function Player({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [speed, setSpeed] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [run, setRun] = useState(0);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let dispose = () => {};
    void (async () => {
      const res = await fetch(`/api/v1/terminal/sessions/${sessionId}/recording`, { cache: "no-store" });
      if (!res.ok) {
        if (!stopped) setError(message(res.status === 404 ? "not_found" : `http_${res.status}`));
        return;
      }
      const lines = (await res.text()).split("\n").filter(Boolean);
      const header = JSON.parse(lines[0]) as { width: number; height: number };
      const events = lines.slice(1).map((l) => JSON.parse(l) as CastEvent);
      const { Terminal } = await import("@xterm/xterm");
      if (stopped || !box.current) return;
      const term = new Terminal({
        theme: TERMINAL_THEME,
        fontFamily: monoFont(),
        fontSize: 12,
        cols: header.width,
        rows: header.height,
        disableStdin: true,
      });
      term.open(box.current);
      dispose = () => term.dispose();
      let i = 0;
      const step = () => {
        if (stopped || i >= events.length) return;
        const [t, kind, data] = events[i++];
        if (kind === "o") term.write(data);
        else if (kind === "r") {
          const [c, r] = data.split("x").map(Number);
          term.resize(c, r);
        }
        const next = events[i];
        if (next) {
          // long pauses are capped at 2 s so idle time does not stall the replay
          const gap = Math.min(next[0] - t, 2) / speed;
          timer = setTimeout(step, gap * 1000);
        }
      };
      step();
    })();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      dispose();
    };
  }, [sessionId, speed, run]);

  return (
    <Modal title="Session replay" onClose={onClose}>
      <div className="space-y-2">
        {error && <Alert>{error}</Alert>}
        <div className="flex items-center gap-1 text-xs">
          {[1, 2, 4, 8].map((s) => (
            <Button key={s} variant={s === speed ? "primary" : "secondary"} onClick={() => setSpeed(s)}>
              {s}×
            </Button>
          ))}
          <Button onClick={() => setRun((r) => r + 1)}>
            <RotateCcw className="h-3 w-3" aria-hidden /> Restart
          </Button>
        </div>
        <div ref={box} className="max-h-[60vh] overflow-auto rounded border border-line bg-canvas p-1" />
      </div>
    </Modal>
  );
}

function duration(s: SessionRow): string {
  if (!s.ended_at) return "open";
  const secs = Math.round((new Date(s.ended_at).getTime() - new Date(s.started_at).getTime()) / 1000);
  return secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

function Sessions() {
  const { data, error } = useResource<SessionRow[]>("/terminal/sessions");
  const [playing, setPlaying] = useState<string | null>(null);
  return (
    <Card title="Recorded sessions">
      {error && <Alert>{error}</Alert>}
      {data && data.length === 0 && <p className="text-sm text-fg-muted">No sessions yet.</p>}
      {data && data.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-line text-fg-muted">
              <tr>
                <th className="py-2 pr-3 font-medium">Started</th>
                <th className="py-2 pr-3 font-medium">User</th>
                <th className="py-2 pr-3 font-medium">Node</th>
                <th className="py-2 pr-3 font-medium">Remote user</th>
                <th className="py-2 pr-3 font-medium">Duration</th>
                <th className="py-2 pr-3 font-medium">End</th>
                <th />
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {data.map((s) => (
                <tr key={s.id}>
                  <td className="whitespace-nowrap py-2 pr-3 text-fg-secondary">{new Date(s.started_at).toLocaleString()}</td>
                  <td className="py-2 pr-3">{s.username ?? "—"}</td>
                  <td className="py-2 pr-3">
                    <div>{s.node_name}</div>
                    <div className="font-mono text-[11px] text-fg-muted">{s.target}</div>
                  </td>
                  <td className="py-2 pr-3 font-mono">{s.remote_user}</td>
                  <td className="py-2 pr-3">{duration(s)}</td>
                  <td className="py-2 pr-3 text-fg-secondary">{s.end_reason ? REASONS[s.end_reason] ?? s.end_reason : ""}</td>
                  <td className="py-2 text-right">
                    {s.recorded && (
                      <Button onClick={() => setPlaying(s.id)}>
                        <Play className="h-3 w-3" aria-hidden /> Replay
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {playing && <Player sessionId={playing} onClose={() => setPlaying(null)} />}
    </Card>
  );
}

function HostKeys() {
  const { can } = useAuth();
  const { data, error, reload } = useResource<HostKey[]>("/terminal/host-keys");
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<HostKey | null>(null);

  async function reset(k: HostKey) {
    const r = await api(`/terminal/host-keys/${k.node_id}`, { method: "DELETE" });
    setActionError(r.ok ? null : message(r.code));
    setConfirm(null);
    reload();
  }

  return (
    <Card title="Pinned host keys">
      <p className="mb-3 text-xs text-fg-secondary">
        The first connection to a node pins its SSH host key; later connections accept only that
        key. Reset a pin only after a legitimate change (e.g. the server was reinstalled).
      </p>
      {(error || actionError) && <Alert>{error ?? actionError}</Alert>}
      <table className="w-full text-left text-xs">
        <thead className="border-b border-line text-fg-muted">
          <tr>
            <th className="py-2 pr-3 font-medium">Node</th>
            <th className="py-2 pr-3 font-medium">Type</th>
            <th className="py-2 pr-3 font-medium">Fingerprint</th>
            <th className="py-2 pr-3 font-medium">Pinned</th>
            <th />
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {(data ?? []).map((k) => (
            <tr key={k.node_id}>
              <td className="py-2 pr-3">{k.node_name}</td>
              <td className="py-2 pr-3 font-mono">{k.key_type}</td>
              <td className="py-2 pr-3 font-mono text-fg-secondary">{k.fingerprint}</td>
              <td className="py-2 pr-3 text-fg-secondary">{new Date(k.first_seen).toLocaleString()}</td>
              <td className="py-2 text-right">
                {can("inventory.manage") && (
                  <Button variant="danger" onClick={() => setConfirm(k)}>
                    Reset
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {confirm && (
        <Modal title={`Reset the host key of ${confirm.node_name}?`} onClose={() => setConfirm(null)}>
          <p className="mb-4 text-sm text-fg-secondary">
            The next connection will trust whatever key the server presents. Do this only if you
            know why the key changed.
          </p>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setConfirm(null)}>Cancel</Button>
            <Button variant="danger" onClick={() => void reset(confirm)}>
              Reset
            </Button>
          </div>
        </Modal>
      )}
    </Card>
  );
}

type View = "terminals" | "files" | "snippets" | "sessions" | "hostkeys";

const LABEL: Record<View, string> = {
  terminals: "Terminals",
  files: "Files",
  snippets: "Snippets",
  sessions: "Recorded sessions",
  hostkeys: "Host keys",
};

function Bastion() {
  const { can } = useAuth();
  const params = useSearchParams();
  const canConnect = can("ssh.connect");
  const canFiles = can("sftp.transfer");
  const tabs: View[] = [
    ...(canConnect ? (["terminals"] as const) : []),
    ...(canFiles ? (["files"] as const) : []),
    ...(canConnect || can("snippets.manage") ? (["snippets"] as const) : []),
    "sessions" as const,
    ...(can("inventory.read") ? (["hostkeys"] as const) : []),
  ];
  const wanted = params.get("view") as View | null;
  const [tab, setTab] = useState<View>(wanted && tabs.includes(wanted) ? wanted : tabs[0]);
  const [filesNode, setFilesNode] = useState<string | null>(wanted === "files" ? params.get("node") : null);
  const nodes = useResource<InventoryNode[]>("/nodes?capability=ssh");
  const snippets: Resource<Snippet[]> = useResource<Snippet[]>("/snippets");

  return (
    <div className="space-y-3">
      <h1 className="flex items-center gap-2 text-lg font-semibold">
        <SquareTerminal className="h-5 w-5 text-primary" aria-hidden /> SSH Bastion
      </h1>
      <div role="tablist" className="flex gap-1 border-b border-line text-xs">
        {tabs.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-3 py-2 ${tab === t ? "border-primary text-primary" : "border-transparent text-fg-nav hover:text-fg"}`}
          >
            {LABEL[t]}
          </button>
        ))}
      </div>
      {/* Terminals and the file browser stay mounted while browsing the other tabs */}
      {canConnect && (
        <div className={tab === "terminals" ? "" : "hidden"}>
          <Terminals
            nodes={nodes.data ?? []}
            snippets={snippets.data ?? []}
            onFiles={
              canFiles
                ? (id) => {
                    setFilesNode(id);
                    setTab("files");
                  }
                : undefined
            }
          />
        </div>
      )}
      {canFiles && (
        <div className={tab === "files" ? "" : "hidden"}>
          <FileBrowser key={filesNode ?? ""} nodes={nodes.data ?? []} initialNode={filesNode} />
        </div>
      )}
      {tab === "snippets" && <SnippetLibrary snippets={snippets} />}
      {tab === "sessions" && <Sessions />}
      {tab === "hostkeys" && <HostKeys />}
    </div>
  );
}

export default function TerminalPage() {
  return (
    <Suspense fallback={null}>
      <Bastion />
    </Suspense>
  );
}
