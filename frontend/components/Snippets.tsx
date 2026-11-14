"use client";

import { Pencil, Plus, Trash2, Users } from "lucide-react";
import { type FormEvent, useState } from "react";

import { Alert, Button, Card, Field, Modal, TextInput } from "@/components/ui";
import { api, message, type Resource } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export type Snippet = {
  id: string;
  name: string;
  description: string | null;
  body: string;
  shared: boolean;
  editable: boolean;
  created_by: string | null;
  updated_at: string;
};

function SnippetDialog({
  snippet,
  onClose,
  onSaved,
}: {
  snippet: Snippet | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { can } = useAuth();
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const body = {
      name: String(form.get("name") ?? "").trim(),
      description: String(form.get("description") ?? "").trim() || null,
      body: String(form.get("body") ?? ""),
      ...(snippet ? {} : { shared: form.get("shared") === "on" }),
    };
    const r = snippet
      ? await api(`/snippets/${snippet.id}`, { method: "PATCH", body })
      : await api("/snippets", { method: "POST", body });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }

  return (
    <Modal title={snippet ? `Edit ${snippet.name}` : "New snippet"} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        {error && <Alert>{error}</Alert>}
        <Field label="Name">
          <TextInput name="name" required maxLength={100} defaultValue={snippet?.name} autoFocus />
        </Field>
        <Field label="Description">
          <TextInput name="description" maxLength={1000} defaultValue={snippet?.description ?? ""} />
        </Field>
        <Field label="Commands" hint="Each line is typed into the terminal followed by Enter.">
          <textarea
            name="body"
            required
            maxLength={16384}
            rows={6}
            defaultValue={snippet?.body}
            spellCheck={false}
            className="w-full rounded border border-line bg-canvas px-3 py-1.5 font-mono text-xs outline-none focus:border-primary"
          />
        </Field>
        {!snippet && can("snippets.manage") && (
          <label className="flex items-center gap-2 text-xs text-fg-secondary">
            <input type="checkbox" name="shared" /> Shared with everyone who can open terminals
          </label>
        )}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary">
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function SnippetLibrary({ snippets }: { snippets: Resource<Snippet[]> }) {
  const [editing, setEditing] = useState<Snippet | "new" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function remove(s: Snippet) {
    if (!window.confirm(`Delete the snippet "${s.name}"?`)) return;
    const r = await api(`/snippets/${s.id}`, { method: "DELETE" });
    setError(r.ok ? null : message(r.code));
    snippets.reload();
  }

  return (
    <Card
      title="Snippets"
      actions={
        <Button variant="primary" onClick={() => setEditing("new")}>
          <Plus className="h-3 w-3" aria-hidden /> New snippet
        </Button>
      }
    >
      <p className="mb-3 text-xs text-fg-secondary">
        Run a snippet from the Terminals tab, in the current terminal or in all broadcast
        terminals. Every run is in the audit log with the exact commands sent.
      </p>
      {(snippets.error || error) && <Alert>{snippets.error ?? error}</Alert>}
      {snippets.data?.length === 0 && <p className="text-sm text-fg-muted">No snippets yet.</p>}
      <div className="divide-y divide-line">
        {(snippets.data ?? []).map((s) => (
          <div key={s.id} className="flex items-start gap-3 py-2">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 text-sm">
                {s.name}
                {s.shared && (
                  <span className="inline-flex items-center gap-1 rounded border border-telemetry/40 px-1.5 text-[10px] uppercase text-telemetry">
                    <Users className="h-2.5 w-2.5" aria-hidden /> shared
                  </span>
                )}
              </div>
              {s.description && <div className="text-xs text-fg-secondary">{s.description}</div>}
              <pre className="mt-1 max-h-24 overflow-auto rounded bg-canvas px-2 py-1 font-mono text-[11px] text-fg-secondary">
                {s.body}
              </pre>
            </div>
            {s.editable && (
              <div className="flex gap-1">
                <Button onClick={() => setEditing(s)} aria-label={`Edit ${s.name}`}>
                  <Pencil className="h-3 w-3" aria-hidden />
                </Button>
                <Button variant="danger" onClick={() => void remove(s)} aria-label={`Delete ${s.name}`}>
                  <Trash2 className="h-3 w-3" aria-hidden />
                </Button>
              </div>
            )}
          </div>
        ))}
      </div>
      {editing && (
        <SnippetDialog
          snippet={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            snippets.reload();
          }}
        />
      )}
    </Card>
  );
}
