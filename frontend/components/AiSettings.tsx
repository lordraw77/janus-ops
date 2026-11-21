"use client";

import { type FormEvent, useState } from "react";

import { Alert, Button, Card, Field, Select, TextInput } from "@/components/ui";
import type { AiConfig, AiSlot, McpConfig } from "@/lib/ai";
import { useLoad } from "@/lib/ansible";
import { api, message } from "@/lib/api";

const TITLES: Record<string, { title: string; hint: string }> = {
  main: { title: "Main model", hint: "Diagnoses and the code generator. Data of the problem or node goes to this endpoint." },
  review: {
    title: "Second-opinion model",
    hint: "Asked only when a person presses “Ask for a second opinion”. Empty URL or key = same as the main model. A different model family compares better.",
  },
};

const SOURCE: Record<string, string> = {
  database: "saved here",
  environment: "from the server environment",
  none: "none",
};

type Notice = { kind: "ok" | "error"; text: string } | null;

function SlotForm({
  slot,
  vault,
  notice,
  setNotice,
  onSaved,
}: {
  slot: AiSlot;
  vault: boolean;
  notice: Notice;
  setNotice: (n: Notice) => void;
  onSaved: () => void;
}) {
  const [baseUrl, setBaseUrl] = useState(slot.base_url ?? "");
  const [model, setModel] = useState(slot.model ?? "");
  const [timeout, setTimeoutS] = useState(slot.timeout_s ? String(slot.timeout_s) : "");
  const [key, setKey] = useState("");
  const [models, setModels] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const info = TITLES[slot.slot];

  async function save(event: FormEvent, clearKey = false) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    const body: Record<string, unknown> = { base_url: baseUrl.trim() || null, model: model.trim() || null };
    if (slot.slot === "main") body.timeout_s = timeout ? Number(timeout) : null;
    if (key.trim()) body.api_key = key.trim();
    if (clearKey) body.clear_key = true;
    const r = await api(`/ai/config/${slot.slot}`, { method: "PUT", body });
    setBusy(false);
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    setKey("");
    setNotice({ kind: "ok", text: "Saved. Takes effect on the next request." });
    onSaved();
  }

  async function listModels() {
    setBusy(true);
    setNotice(null);
    const r = await api<{ models: string[] }>(`/ai/config/${slot.slot}/models`, {
      method: "POST",
      body: { base_url: baseUrl.trim() || null, api_key: key.trim() || null },
    });
    setBusy(false);
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    setModels(r.data.models);
  }

  async function test() {
    setBusy(true);
    setNotice(null);
    const r = await api<{ ok: boolean; error: string | null; ms: number; model: string | null }>(
      `/ai/config/${slot.slot}/test`,
      { method: "POST" },
    );
    setBusy(false);
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    setNotice(
      r.data.ok
        ? { kind: "ok", text: `${r.data.model} answered in ${(r.data.ms / 1000).toFixed(1)} s.` }
        : { kind: "error", text: message(r.data.error ?? "run_error") },
    );
  }

  async function reset() {
    if (!window.confirm("Forget what is saved here for this model and go back to the server environment?")) return;
    setBusy(true);
    const r = await api(`/ai/config/${slot.slot}`, { method: "DELETE" });
    setBusy(false);
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    setNotice({ kind: "ok", text: "Back to the environment values." });
    onSaved();
  }

  return (
    <form onSubmit={(e) => void save(e)} className="space-y-3 rounded border border-line p-3">
      <div>
        <p className="font-medium">{info.title}</p>
        <p className="text-xs text-fg-muted">{info.hint}</p>
      </div>
      {notice && <Alert kind={notice.kind}>{notice.text}</Alert>}
      <Field label="Endpoint URL" hint="OpenAI-compatible, https only (for example https://api.groq.com/openai/v1)">
        <TextInput value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={slot.slot === "review" ? "same as the main model" : "https://…/v1"} />
      </Field>
      <Field label="Model">
        <div className="flex gap-2">
          <TextInput className="flex-1" value={model} onChange={(e) => setModel(e.target.value)} />
          <Button type="button" disabled={busy} onClick={() => void listModels()}>
            List models
          </Button>
        </div>
        {models && (
          <Select
            className="mt-2"
            value={models.includes(model) ? model : ""}
            onChange={(e) => e.target.value && setModel(e.target.value)}
          >
            <option value="">{models.length} models offered: choose one</option>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </Select>
        )}
      </Field>
      {slot.slot === "main" && (
        <Field label="Timeout (seconds)" hint="5–900; a reasoning model can need minutes. Empty = environment value">
          <TextInput type="number" min={5} max={900} value={timeout} onChange={(e) => setTimeoutS(e.target.value)} />
        </Field>
      )}
      <Field
        label="API key"
        hint={`Now: ${slot.key_set ? "set" : "not set"} (${SOURCE[slot.key_source]}). It is stored encrypted and never shown again.`}
      >
        <TextInput
          type="password"
          autoComplete="off"
          value={key}
          disabled={!vault}
          placeholder={slot.key_set ? "leave empty to keep the current key" : ""}
          onChange={(e) => setKey(e.target.value)}
        />
      </Field>
      {!vault && <Alert kind="info">{message("vault_unavailable")}</Alert>}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" disabled={busy}>
          Save
        </Button>
        <Button type="button" disabled={busy || !slot.model} onClick={() => void test()}>
          Test
        </Button>
        {slot.key_source === "database" && (
          <Button type="button" variant="danger" disabled={busy} onClick={(e) => void save(e, true)}>
            Remove saved key
          </Button>
        )}
        {slot.in_database && (
          <Button type="button" disabled={busy} onClick={() => void reset()}>
            Back to environment
          </Button>
        )}
      </div>
    </form>
  );
}

const REASONS: Record<string, string> = {
  ok: "ready",
  no_credential: "no usable credential",
  jump_host_unsupported: "behind a jump host (not supported)",
};

export function McpSettingsCard() {
  const mcp = useLoad<McpConfig>("/ai/config/mcp");
  const [enabled, setEnabled] = useState<"env" | "on" | "off">("env");
  const [allowWrite, setAllowWrite] = useState(true);
  const [planOnly, setPlanOnly] = useState<string[]>([]);
  const [loaded, setLoaded] = useState<McpConfig | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);

  if (mcp.data && mcp.data !== loaded) {
    setLoaded(mcp.data);
    setEnabled(mcp.data.enabled_source === "environment" ? "env" : mcp.data.enabled ? "on" : "off");
    setAllowWrite(mcp.data.allow_write);
    setPlanOnly(mcp.data.plan_only_environments);
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    const r = await api("/ai/config/mcp", {
      method: "PUT",
      body: {
        enabled: enabled === "env" ? null : enabled === "on",
        allow_write: allowWrite,
        plan_only_environments: planOnly,
      },
    });
    setBusy(false);
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    setNotice({ kind: "ok", text: "Saved. Takes effect on the next diagnosis." });
    mcp.reload();
  }

  const d = mcp.data;
  return (
    <Card title="Copilot tools (MCP)">
      <form onSubmit={(e) => void save(e)} className="space-y-3 text-sm">
        <p className="text-xs text-fg-secondary">
          Lets the Copilot look at a node (and, with the permission “ai.operate”, act on it) through the linux-mcp and
          proxmox-mcp containers, started on this host&apos;s Docker for each diagnosis. It sees only the node of the problem.
        </p>
        {mcp.error && <Alert>{mcp.error}</Alert>}
        {notice && <Alert kind={notice.kind === "ok" ? "info" : "error"}>{notice.text}</Alert>}
        {d && (
          <>
            <p className="text-xs">
              Docker:{" "}
              {d.docker_version ? (
                <span className="text-ok">reachable (engine {d.docker_version})</span>
              ) : (
                <span className="text-crit">not reachable: add deploy/compose.mcp.yaml to the stack</span>
              )}
              {" · "}network: {d.network ?? "Docker default"} · images: {d.linux_image}, {d.proxmox_image}
            </p>
            <Field label="Tools">
              <Select value={enabled} onChange={(e) => setEnabled(e.target.value as "env" | "on" | "off")}>
                <option value="env">Follow the server environment (currently {d.enabled ? "on" : "off"})</option>
                <option value="on">On</option>
                <option value="off">Off</option>
              </Select>
            </Field>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={allowWrite} onChange={(e) => setAllowWrite(e.target.checked)} />
              Allow operational tools (restart a service, power a VM, run a command) for users with “ai.operate”
            </label>
            <Field label="Read-only in these environments (ticked = the AI cannot change servers there, it only proposes). Untick to allow changes">
              <div className="flex flex-wrap gap-3">
                {d.environments.map((env) => (
                  <label key={env} className="flex items-center gap-1">
                    <input
                      type="checkbox"
                      checked={planOnly.includes(env)}
                      onChange={(e) => setPlanOnly(e.target.checked ? [...planOnly, env] : planOnly.filter((x) => x !== env))}
                    />
                    {env}
                  </label>
                ))}
              </div>
            </Field>
            <Button type="submit" variant="primary" disabled={busy}>
              Save
            </Button>
            <div>
              <p className="mb-1 text-xs font-medium">Nodes the tools can reach ({d.nodes.length})</p>
              <ul className="max-h-56 overflow-auto text-xs">
                {d.nodes.map((n) => (
                  <li key={n.name} className="flex gap-3 border-b border-border py-1">
                    <span className="w-40 font-mono">{n.name}</span>
                    <span className="w-24 text-fg-muted">{n.environment}</span>
                    <span>Linux: {n.linux ? (REASONS[n.linux] ?? n.linux) : "—"}</span>
                    <span>Proxmox: {n.proxmox ? (REASONS[n.proxmox] ?? n.proxmox) : "—"}</span>
                  </li>
                ))}
              </ul>
            </div>
          </>
        )}
      </form>
    </Card>
  );
}

export function AiSettingsCard() {
  const config = useLoad<AiConfig>("/ai/config");
  const [notices, setNotices] = useState<Record<string, Notice>>({});
  return (
    <Card title="AI models and keys">
      <div className="space-y-3 text-sm">
        <p className="text-xs text-fg-secondary">
          What is saved here wins over the JANUS_AI_* values in the server environment; empty fields fall back to them. The
          “Test” button sends one tiny request with the saved settings.
        </p>
        {config.error && <Alert>{config.error}</Alert>}
        <div className="grid gap-4 lg:grid-cols-2">
          {(config.data?.slots ?? []).map((s) => (
            <SlotForm
              // the form remounts when the slot goes back to the environment, so it shows those values
              key={`${s.slot}-${s.in_database ? "saved" : "env"}`}
              slot={s}
              vault={config.data?.vault_available ?? false}
              notice={notices[s.slot] ?? null}
              setNotice={(n) => setNotices((cur) => ({ ...cur, [s.slot]: n }))}
              onSaved={config.reload}
            />
          ))}
        </div>
      </div>
    </Card>
  );
}
