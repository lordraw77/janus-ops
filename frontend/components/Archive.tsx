"use client";

import { useEffect, useMemo, useState } from "react";

import { Bubble } from "@/components/Operate";
import { Player } from "@/components/SessionPlayer";
import { Alert, Button, Card, Field, Modal, Select, TextInput } from "@/components/ui";
import { api, message } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { type ArchivePage, type ChatItem, type RecordingItem, type Retention, whenExpires } from "@/lib/archive";
import type { OperateMessage } from "@/lib/operate";

const PAGE = 50;
type Kind = "recordings" | "chats";
type Notice = { kind: "ok" | "error"; text: string } | null;

function RetentionCard() {
  const { can } = useAuth();
  const manage = can("archive.manage");
  const [saved, setSaved] = useState<Retention | null>(null);
  const [x, setX] = useState("1");
  const [y, setY] = useState("30");
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void api<Retention>("/archive/retention").then((r) => {
      if (!r.ok) return;
      setSaved(r.data);
      setX(String(r.data.archive_after_days));
      setY(String(r.data.delete_after_days));
    });
  }, []);

  async function save() {
    setBusy(true);
    setNotice(null);
    const body = { archive_after_days: Number(x), delete_after_days: Number(y) };
    const r = await api("/archive/retention", { method: "PUT", body });
    setBusy(false);
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    setSaved(body);
    setNotice({ kind: "ok", text: "Saved. It applies at the next hourly run, or press “Apply now”." });
  }

  async function applyNow() {
    if (!window.confirm("Apply the retention now? Items older than the delete limit are removed for good (except the ones marked to keep).")) return;
    setBusy(true);
    const r = await api<Record<string, number>>("/archive/run", { method: "POST" });
    setBusy(false);
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    const d = r.data;
    setNotice({
      kind: "ok",
      text: `Archived ${d.archived_recordings} recording(s) and ${d.archived_chats} chat(s); deleted ${d.deleted_recordings} and ${d.deleted_chats}.`,
    });
  }

  const dirty = saved !== null && (Number(x) !== saved.archive_after_days || Number(y) !== saved.delete_after_days);
  return (
    <Card title="Retention">
      <div className="space-y-3 text-sm">
        <p className="text-xs text-fg-secondary">
          Recorded SSH sessions and Operate chats are looked after in two steps, counted in days from when they ended: after the first limit they
          move here (out of the everyday lists, still searchable); after the second they are deleted. An item marked “keep” is never deleted
          automatically.
        </p>
        {notice && <Alert kind={notice.kind === "ok" ? "ok" : "error"}>{notice.text}</Alert>}
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Archive after (days)">
            <TextInput type="number" min={0} max={3650} className="w-28" value={x} disabled={!manage} onChange={(e) => setX(e.target.value)} />
          </Field>
          <Field label="Delete after (days)">
            <TextInput type="number" min={1} max={3650} className="w-28" value={y} disabled={!manage} onChange={(e) => setY(e.target.value)} />
          </Field>
          {manage && (
            <>
              <Button variant="primary" disabled={busy || !dirty} onClick={() => void save()}>
                Save
              </Button>
              <Button disabled={busy || dirty} onClick={() => void applyNow()}>
                Apply now
              </Button>
            </>
          )}
        </div>
      </div>
    </Card>
  );
}

function ChatView({ id, onClose }: { id: string; onClose: () => void }) {
  const [chat, setChat] = useState<{ title: string; node_names: string[]; created_by: string; messages: OperateMessage[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void api<typeof chat>(`/archive/chats/${id}`).then((r) => (r.ok ? setChat(r.data) : setError(message(r.code))));
  }, [id]);
  return (
    <Modal title={chat ? `Chat: ${chat.title}` : "Chat"} onClose={onClose}>
      <div className="space-y-3">
        {error && <Alert>{error}</Alert>}
        {chat && (
          <p className="text-xs text-fg-muted">
            {chat.created_by} · {chat.node_names.join(", ")}
          </p>
        )}
        <div className="max-h-[65vh] space-y-3 overflow-auto">
          {(chat?.messages ?? []).map((m) => (
            <Bubble key={m.id} m={m} />
          ))}
        </div>
      </div>
    </Modal>
  );
}

function duration(r: RecordingItem): string {
  if (!r.ended_at) return "open";
  const secs = Math.round((new Date(r.ended_at).getTime() - new Date(r.started_at).getTime()) / 1000);
  return secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

function ArchiveList({ kind }: { kind: Kind }) {
  const { can } = useAuth();
  const manage = can("archive.manage");
  const [q, setQ] = useState("");
  const [inContent, setInContent] = useState(false);
  const [keep, setKeep] = useState<"" | "true" | "false">("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [applied, setApplied] = useState({ q: "", inContent: false, keep: "", since: "", until: "" });
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<ArchivePage<RecordingItem | ChatItem> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [notice, setNotice] = useState<Notice>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  const [viewing, setViewing] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  const query = useMemo(() => {
    const p = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (applied.q.trim()) p.set("q", applied.q.trim());
    if (kind === "recordings" && applied.inContent && applied.q.trim()) p.set("in_content", "true");
    if (applied.keep) p.set("keep", applied.keep);
    if (applied.since) p.set("since", new Date(applied.since).toISOString());
    if (applied.until) p.set("until", new Date(`${applied.until}T23:59:59`).toISOString());
    return `/archive/${kind}?${p.toString()}`;
  }, [applied, kind, offset]);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    void api<ArchivePage<RecordingItem | ChatItem>>(query).then((r) => {
      if (cancelled) return;
      setLoading(false);
      if (r.ok) {
        setPage(r.data);
        setError(null);
      } else setError(message(r.code));
    });
    return () => {
      cancelled = true;
    };
  }, [query, version]);

  const items = page?.items ?? [];
  const allPicked = items.length > 0 && items.every((i) => picked.includes(i.id));

  function search() {
    setOffset(0);
    setPicked([]);
    setApplied({ q, inContent, keep, since, until });
  }

  async function bulk(action: "delete" | "keep" | "unkeep", ids: string[]) {
    if (ids.length === 0) return;
    if (action === "delete" && !window.confirm(`Delete ${ids.length} ${kind === "chats" ? "chat" : "recording"}${ids.length === 1 ? "" : "s"} for good?`)) return;
    setNotice(null);
    const r =
      action === "delete"
        ? await api<{ deleted: number; protected: number; not_found: number }>("/archive/delete", { method: "POST", body: { kind, ids } })
        : await api<{ updated: number }>("/archive/keep", { method: "POST", body: { kind, ids, keep: action === "keep" } });
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    if (action === "delete") {
      const d = r.data as { deleted: number; protected: number };
      setNotice({
        kind: d.protected ? "error" : "ok",
        text: `Deleted ${d.deleted}.${d.protected ? ` ${d.protected} marked “keep” were not deleted: unmark them first.` : ""}`,
      });
    } else setNotice({ kind: "ok", text: action === "keep" ? "Marked to keep." : "No longer marked to keep." });
    setPicked([]);
    setVersion((v) => v + 1);
  }

  const total = page?.total ?? 0;
  return (
    <div className="space-y-3">
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          search();
        }}
      >
        <Field label="Search" hint={kind === "recordings" ? "User, node, target or remote user" : "Title, user, servers or anything said in the chat"}>
          <TextInput className="w-72" value={q} onChange={(e) => setQ(e.target.value)} />
        </Field>
        <Field label="Keep">
          <Select value={keep} onChange={(e) => setKeep(e.target.value as "" | "true" | "false")}>
            <option value="">All</option>
            <option value="true">Marked to keep</option>
            <option value="false">Not marked</option>
          </Select>
        </Field>
        <Field label="From">
          <TextInput type="date" value={since} onChange={(e) => setSince(e.target.value)} />
        </Field>
        <Field label="To">
          <TextInput type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
        </Field>
        <Button type="submit" variant="primary">
          Search
        </Button>
        {kind === "recordings" && (
          <label className="flex items-center gap-1 pb-2 text-xs">
            <input type="checkbox" checked={inContent} onChange={(e) => setInContent(e.target.checked)} />
            also inside the recorded output (slower)
          </label>
        )}
      </form>
      {error && <Alert>{error}</Alert>}
      {notice && <Alert kind={notice.kind === "ok" ? "ok" : "error"}>{notice.text}</Alert>}
      {page?.searched_content && <p className="text-xs text-fg-muted">Searched inside the most recent recordings only (a few hundred, first 4 MB of each).</p>}
      {manage && picked.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded border border-line bg-elevated p-2 text-xs">
          <span>{picked.length} selected</span>
          <Button onClick={() => void bulk("keep", picked)}>Mark to keep</Button>
          <Button onClick={() => void bulk("unkeep", picked)}>Unmark</Button>
          <Button variant="danger" onClick={() => void bulk("delete", picked)}>
            Delete
          </Button>
          <Button onClick={() => setPicked([])}>Clear</Button>
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="border-b border-line text-fg-muted">
            <tr>
              {manage && (
                <th className="w-6 py-2 pr-2">
                  <input
                    type="checkbox"
                    aria-label="Select all on this page"
                    checked={allPicked}
                    onChange={(e) => setPicked(e.target.checked ? items.map((i) => i.id) : [])}
                  />
                </th>
              )}
              {kind === "recordings" ? (
                <>
                  <th className="py-2 pr-3 font-medium">Started</th>
                  <th className="py-2 pr-3 font-medium">User</th>
                  <th className="py-2 pr-3 font-medium">Node</th>
                  <th className="py-2 pr-3 font-medium">Remote user</th>
                  <th className="py-2 pr-3 font-medium">Duration</th>
                </>
              ) : (
                <>
                  <th className="py-2 pr-3 font-medium">Last message</th>
                  <th className="py-2 pr-3 font-medium">Chat</th>
                  <th className="py-2 pr-3 font-medium">User</th>
                  <th className="py-2 pr-3 font-medium">Servers</th>
                  <th className="py-2 pr-3 font-medium">Messages</th>
                </>
              )}
              <th className="py-2 pr-3 font-medium">Archived</th>
              <th className="py-2 pr-3 font-medium">Deleted</th>
              <th />
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {items.map((it) => {
              const rec = kind === "recordings" ? (it as RecordingItem) : null;
              const chat = kind === "chats" ? (it as ChatItem) : null;
              return (
                <tr key={it.id}>
                  {manage && (
                    <td className="py-2 pr-2">
                      <input
                        type="checkbox"
                        aria-label="Select"
                        checked={picked.includes(it.id)}
                        onChange={(e) => setPicked(e.target.checked ? [...picked, it.id] : picked.filter((x) => x !== it.id))}
                      />
                    </td>
                  )}
                  {rec && (
                    <>
                      <td className="whitespace-nowrap py-2 pr-3 text-fg-secondary">{new Date(rec.started_at).toLocaleString()}</td>
                      <td className="py-2 pr-3">{rec.username ?? "—"}</td>
                      <td className="py-2 pr-3">
                        <div>{rec.node_name}</div>
                        <div className="font-mono text-[11px] text-fg-muted">{rec.target}</div>
                      </td>
                      <td className="py-2 pr-3 font-mono">{rec.remote_user}</td>
                      <td className="py-2 pr-3">{duration(rec)}</td>
                    </>
                  )}
                  {chat && (
                    <>
                      <td className="whitespace-nowrap py-2 pr-3 text-fg-secondary">{new Date(chat.updated_at).toLocaleString()}</td>
                      <td className="max-w-64 truncate py-2 pr-3" title={chat.title}>
                        {chat.title}
                      </td>
                      <td className="py-2 pr-3">{chat.created_by}</td>
                      <td className="max-w-48 truncate py-2 pr-3 font-mono" title={chat.node_names.join(", ")}>
                        {chat.node_names.join(", ")}
                      </td>
                      <td className="py-2 pr-3">{chat.messages}</td>
                    </>
                  )}
                  <td className="whitespace-nowrap py-2 pr-3 text-fg-secondary">{new Date(it.archived_at).toLocaleDateString()}</td>
                  <td className={`whitespace-nowrap py-2 pr-3 ${it.keep ? "text-ok" : "text-fg-secondary"}`}>{whenExpires(it)}</td>
                  <td className="space-x-1 whitespace-nowrap py-2 text-right">
                    {rec?.recorded && <Button onClick={() => setPlaying(rec.id)}>Replay</Button>}
                    {rec && !rec.recorded && <span className="text-fg-muted">no recording file</span>}
                    {chat && <Button onClick={() => setViewing(chat.id)}>View</Button>}
                    {manage && <Button onClick={() => void bulk(it.keep ? "unkeep" : "keep", [it.id])}>{it.keep ? "Unkeep" : "Keep"}</Button>}
                    {manage && (
                      <Button variant="danger" disabled={it.keep} title={it.keep ? "Unmark it first" : ""} onClick={() => void bulk("delete", [it.id])}>
                        Delete
                      </Button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {items.length === 0 && !loading && (
          <p className="py-3 text-sm text-fg-muted">
            {applied.q || applied.keep || applied.since || applied.until ? "Nothing matches." : `No archived ${kind === "chats" ? "chats" : "recordings"} yet.`}
          </p>
        )}
      </div>
      <div className="flex items-center gap-2 text-xs text-fg-muted">
        <span>
          {total === 0 ? "0" : `${offset + 1}–${Math.min(offset + PAGE, total)}`} of {total}
        </span>
        <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
          Previous
        </Button>
        <Button disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>
          Next
        </Button>
      </div>
      {playing && <Player sessionId={playing} onClose={() => setPlaying(null)} />}
      {viewing && <ChatView id={viewing} onClose={() => setViewing(null)} />}
    </div>
  );
}

export function ArchiveConsole() {
  const [kind, setKind] = useState<Kind>("recordings");
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Archive</h1>
      <RetentionCard />
      <div className="flex gap-1 border-b border-line" role="tablist">
        {(["recordings", "chats"] as const).map((k) => (
          <button
            key={k}
            type="button"
            role="tab"
            aria-selected={kind === k}
            onClick={() => setKind(k)}
            className={`-mb-px border-b-2 px-3 py-1.5 text-xs font-semibold ${kind === k ? "border-primary text-primary" : "border-transparent text-fg-muted hover:text-fg"}`}
          >
            {k === "recordings" ? "Recorded sessions" : "Operate chats"}
          </button>
        ))}
      </div>
      <Card>
        <ArchiveList key={kind} kind={kind} />
      </Card>
    </div>
  );
}
