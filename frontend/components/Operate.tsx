"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { Alert, Button, Card, TextInput } from "@/components/ui";
import { useLoad } from "@/lib/ansible";
import { api, message } from "@/lib/api";
import {
  EXAMPLES,
  type OperableNode,
  type OperateMessage,
  type OperateSession,
  type OperateSessionRow,
  type OperateStep,
  type Provider,
  ready,
} from "@/lib/operate";

const REASONS: Record<string, string> = {
  no_credential: "no usable credential",
  jump_host_unsupported: "behind a jump host (not supported)",
};

function Picker({ onStart }: { onStart: (id: string) => void }) {
  const nodes = useLoad<OperableNode[]>("/ai/operate/nodes");
  const [filter, setFilter] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return (nodes.data ?? []).filter((n) => !q || n.name.toLowerCase().includes(q) || n.environment.toLowerCase().includes(q));
  }, [nodes.data, filter]);

  async function start() {
    setBusy(true);
    setError(null);
    const r = await api<{ id: string }>("/ai/operate/sessions", { method: "POST", body: { node_ids: picked } });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    onStart(r.data.id);
  }

  return (
    <Card title="Choose the servers">
      <div className="space-y-3 text-sm">
        <p className="text-xs text-fg-secondary">
          Pick one or more servers (up to 10), then tell the assistant what to do in plain words. It can look at them and,
          because you hold “ai.operate”, change them. Everything it does is listed and audited.
        </p>
        {nodes.error && <Alert>{nodes.error}</Alert>}
        {error && <Alert>{error}</Alert>}
        <TextInput placeholder="Filter by name or environment" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <ul className="max-h-96 overflow-auto">
          {shown.map((n) => {
            const ok = ready(n);
            const why = [n.linux, n.proxmox].filter((x) => x && x !== "ok").map((x) => REASONS[x ?? ""] ?? x).join(", ");
            return (
              <li key={n.id} className="border-b border-border py-1">
                <label className={`flex items-center gap-2 ${ok ? "" : "opacity-50"}`}>
                  <input
                    type="checkbox"
                    disabled={!ok || (!picked.includes(n.id) && picked.length >= 10)}
                    checked={picked.includes(n.id)}
                    onChange={(e) => setPicked(e.target.checked ? [...picked, n.id] : picked.filter((x) => x !== n.id))}
                  />
                  <span className="w-48 font-mono">{n.name}</span>
                  <span className="w-28 text-fg-muted">{n.environment}</span>
                  <span className="text-xs text-fg-muted">
                    {[n.linux === "ok" ? "Linux" : null, n.proxmox === "ok" ? "Proxmox" : null].filter(Boolean).join(" + ")}
                    {!ok && why}
                  </span>
                </label>
              </li>
            );
          })}
          {nodes.data && shown.length === 0 && <li className="py-2 text-fg-muted">No server matches.</li>}
        </ul>
        <Button variant="primary" disabled={busy || picked.length === 0} onClick={() => void start()}>
          Start chat with {picked.length} server{picked.length === 1 ? "" : "s"}
        </Button>
      </div>
    </Card>
  );
}

function Steps({ steps }: { steps: OperateStep[] }) {
  if (steps.length === 0) return null;
  const changed = steps.filter((s) => s.mode === "write");
  return (
    <details className="mt-2 text-xs text-fg-muted" open={changed.length > 0}>
      <summary className="cursor-pointer">
        {steps.length} tool call{steps.length === 1 ? "" : "s"}
        {changed.length > 0 ? `, ${changed.length} with tools that can change things` : ""}
      </summary>
      <ol className="mt-2 space-y-2">
        {steps.map((s) => (
          <li key={s.seq} className="rounded bg-elevated p-2">
            <p className="font-mono">
              {s.mode === "write" && <span className="mr-1 rounded bg-warn/20 px-1 text-warn">can change</span>}
              {s.target && <span className="mr-1 text-fg-secondary">[{s.target}]</span>}
              {s.tool} {JSON.stringify(s.arguments)}
              <span className="ml-2 text-fg-muted">{s.duration_ms ?? 0} ms</span>
            </p>
            {s.error && <p className="text-crit">{message(s.error)}</p>}
            {s.result && <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap">{s.result}</pre>}
          </li>
        ))}
      </ol>
    </details>
  );
}

function Bubble({ m }: { m: OperateMessage }) {
  if (m.role === "user") {
    return (
      <div className="ml-auto max-w-[85%] whitespace-pre-wrap rounded bg-primary/15 p-3 text-sm">{m.content}</div>
    );
  }
  return (
    <div className="mr-auto max-w-[95%] rounded border border-border p-3 text-sm">
      {m.model && <p className="mb-1 text-xs text-fg-muted">{m.model}</p>}
      {m.status === "pending" && <p className="text-fg-muted">Working… (it looks, changes if needed, then verifies)</p>}
      {m.status === "error" && <Alert>{message(m.error ?? "run_error")}{m.content ? ` — ${m.content}` : ""}</Alert>}
      {m.content && m.status !== "error" && <div className="whitespace-pre-wrap">{m.content}</div>}
      {m.read_only && m.status === "done" && (
        <p className="mt-2 text-xs text-warn">
          Read-only answer: operational tools were not offered. Check Settings → Copilot tools: the switch “Allow
          operational tools” must be on and none of these servers&apos; environments may be ticked under “Action plan
          only”. You also need the “ai.operate” permission.
        </p>
      )}
      <Steps steps={m.steps} />
    </div>
  );
}

function ModelPicker({ session, onChanged }: { session: OperateSession; onChanged: () => void }) {
  const providers = useLoad<Provider[]>("/ai/operate/providers");
  const [error, setError] = useState<string | null>(null);
  const slot = session.provider;
  const loaded = useLoad<string[]>(`/ai/operate/providers/${slot}/models`);
  const models = loaded.data ?? (loaded.error ? [] : null);

  async function patch(body: Record<string, unknown>) {
    setError(null);
    const r = await api(`/ai/operate/sessions/${session.id}`, { method: "PATCH", body });
    if (!r.ok) return setError(message(r.code));
    onChanged();
  }

  const current = providers.data?.find((p) => p.slot === slot);
  const options = models ?? [];
  const selected = session.model ?? current?.model ?? "";
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="text-fg-muted">Provider</span>
      <select
        className="rounded border border-border bg-surface px-1 py-1"
        value={slot}
        onChange={(e) => void patch({ provider: e.target.value, clear_model: true })}
      >
        {(providers.data ?? []).map((p) => (
          <option key={p.slot} value={p.slot} disabled={!p.available}>
            {p.label}
            {p.host ? ` · ${p.host}` : ""}
            {p.available ? "" : " (not set up)"}
          </option>
        ))}
      </select>
      <span className="text-fg-muted">Model</span>
      <select
        className="max-w-64 rounded border border-border bg-surface px-1 py-1"
        value={selected}
        onChange={(e) => void patch(e.target.value === (current?.model ?? "") ? { clear_model: true } : { model: e.target.value })}
      >
        {selected && !options.includes(selected) && <option value={selected}>{selected}</option>}
        {current?.model && !options.includes(current.model) && selected !== current.model && (
          <option value={current.model}>{current.model} (default)</option>
        )}
        {options.map((m) => (
          <option key={m} value={m}>
            {m}
            {m === current?.model ? " (default)" : ""}
          </option>
        ))}
      </select>
      {models === null && <span className="text-fg-muted">loading models…</span>}
      {error && <span className="text-crit">{error}</span>}
      <span className="text-fg-muted">Applies from the next message.</span>
    </div>
  );
}

function Chat({ id, onDeleted, onChanged }: { id: string; onDeleted: () => void; onChanged: () => void }) {
  const session = useLoad<OperateSession>(`/ai/operate/sessions/${id}`);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const pending = session.data?.messages.some((m) => m.status === "pending") ?? false;

  useEffect(() => {
    if (!pending) return;
    const t = setInterval(session.reload, 2500);
    return () => clearInterval(t);
  }, [pending, session.reload]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [session.data?.messages.length, pending]);

  async function send(content: string) {
    if (!content.trim()) return;
    setBusy(true);
    setError(null);
    const r = await api(`/ai/operate/sessions/${id}/messages`, { method: "POST", body: { content } });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    setText("");
    session.reload();
  }

  async function setArchived(archived: boolean) {
    const r = await api(`/ai/operate/sessions/${id}`, { method: "PATCH", body: { archived } });
    if (r.ok) {
      session.reload();
      onChanged();
    }
  }

  async function rename() {
    const title = window.prompt("Chat name", session.data?.title ?? "");
    if (!title?.trim()) return;
    const r = await api(`/ai/operate/sessions/${id}`, { method: "PATCH", body: { title: title.trim() } });
    if (r.ok) {
      session.reload();
      onChanged();
    }
  }

  async function remove() {
    if (!window.confirm("Delete this chat? The audit log keeps what was done.")) return;
    const r = await api(`/ai/operate/sessions/${id}`, { method: "DELETE" });
    if (r.ok) onDeleted();
  }

  const d = session.data;
  return (
    <Card
      title={d ? `Chat: ${d.node_names.join(", ")}` : "Chat"}
      actions={
        <div className="flex gap-2">
          <Button onClick={() => void rename()}>Rename</Button>
          <Button onClick={() => void setArchived(!d?.archived)}>{d?.archived ? "Unarchive" : "Archive"}</Button>
          <Button variant="danger" onClick={() => void remove()}>
            Delete
          </Button>
        </div>
      }
    >
      <div className="space-y-3">
        {session.error && <Alert>{session.error}</Alert>}
        {d && <ModelPicker session={d} onChanged={() => { session.reload(); onChanged(); }} />}
        {d?.archived && <Alert kind="info">This chat is archived. You can still read it and continue it; unarchive to bring it back to the list.</Alert>}
        <div className="max-h-[60vh] min-h-48 space-y-3 overflow-auto">
          {(d?.messages ?? []).length === 0 && (
            <div className="space-y-2 text-sm text-fg-secondary">
              <p>Tell the assistant what to do on these servers. For example:</p>
              <div className="flex flex-wrap gap-2">
                {EXAMPLES.map((ex) => (
                  <button
                    key={ex}
                    type="button"
                    className="rounded border border-border px-2 py-1 text-left text-xs hover:bg-elevated"
                    onClick={() => setText(ex)}
                  >
                    {ex}
                  </button>
                ))}
              </div>
            </div>
          )}
          {(d?.messages ?? []).map((m) => (
            <Bubble key={m.id} m={m} />
          ))}
          <div ref={endRef} />
        </div>
        {error && <Alert>{error}</Alert>}
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void send(text);
          }}
        >
          <textarea
            className="min-h-16 flex-1 rounded border border-border bg-surface p-2 text-sm"
            placeholder="e.g. change the NTP server to ntp.example.org on all of them"
            value={text}
            disabled={busy || pending}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                void send(text);
              }
            }}
          />
          <Button type="submit" variant="primary" disabled={busy || pending || !text.trim()}>
            Send
          </Button>
        </form>
        <p className="text-xs text-fg-muted">Ctrl+Enter sends. Changes happen right away on the chosen servers.</p>
      </div>
    </Card>
  );
}

export function OperateConsole() {
  const [showArchived, setShowArchived] = useState(false);
  const sessions = useLoad<OperateSessionRow[]>(`/ai/operate/sessions?archived=${showArchived}`);
  const [current, setCurrent] = useState<string | "new" | null>(null);

  const active = current ?? "new";
  return (
    <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
      <Card
        title="Chats"
        actions={
          <Button onClick={() => setCurrent("new")} disabled={active === "new"}>
            New
          </Button>
        }
      >
        {sessions.error && <Alert>{sessions.error}</Alert>}
        <div className="mb-2 flex gap-1 text-xs">
          {[false, true].map((a) => (
            <button
              key={String(a)}
              type="button"
              className={`rounded px-2 py-1 ${showArchived === a ? "bg-elevated font-medium" : "text-fg-muted hover:bg-elevated"}`}
              onClick={() => setShowArchived(a)}
            >
              {a ? "Archived" : "Active"}
            </button>
          ))}
        </div>
        <ul className="space-y-1 text-sm">
          {(sessions.data ?? []).map((s) => (
            <li key={s.id}>
              <button
                type="button"
                className={`w-full rounded px-2 py-1 text-left hover:bg-elevated ${active === s.id ? "bg-elevated" : ""}`}
                onClick={() => setCurrent(s.id)}
              >
                <span className="block truncate">{s.title}</span>
                <span className="text-xs text-fg-muted">{new Date(s.updated_at).toLocaleString()}</span>
              </button>
            </li>
          ))}
          {sessions.data && sessions.data.length === 0 && (
            <li className="text-fg-muted">{showArchived ? "No archived chats." : "No chats yet."}</li>
          )}
        </ul>
      </Card>
      {active === "new" ? (
        <Picker
          onStart={(id) => {
            sessions.reload();
            setCurrent(id);
          }}
        />
      ) : (
        <Chat
          key={active}
          id={active}
          onChanged={sessions.reload}
          onDeleted={() => {
            sessions.reload();
            setCurrent("new");
          }}
        />
      )}
    </div>
  );
}
