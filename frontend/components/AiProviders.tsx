"use client";

import { useState } from "react";

import { SortTh } from "@/components/SortTh";

import { Alert, Button, Card, Field, Select, TextInput } from "@/components/ui";
import type { AiProvider, AiRoute, HealthRow, Preset, ProviderModel, UsageRow } from "@/lib/aiProviders";
import { useLoad } from "@/lib/ansible";
import { api, message } from "@/lib/api";
import { type Getters, useSortedRows } from "@/lib/sort";

type Notice = { kind: "ok" | "error"; text: string } | null;

function NoticeBar({ notice }: { notice: Notice }) {
  return notice ? <Alert kind={notice.kind === "ok" ? "ok" : "error"}>{notice.text}</Alert> : null;
}

function ModelRow({ provider, m, onChanged, setNotice }: { provider: AiProvider; m: ProviderModel; onChanged: () => void; setNotice: (n: Notice) => void }) {
  const [busy, setBusy] = useState(false);
  const path = `/ai/providers/${provider.id}/models/${m.model_id.split("/").map(encodeURIComponent).join("/")}`;

  async function test() {
    setBusy(true);
    setNotice(null);
    const r = await api<{ ok: boolean; tools: boolean; error: string; latency_ms: number }>(`${path}/test`, { method: "POST" });
    setBusy(false);
    if (!r.ok) setNotice({ kind: "error", text: message(r.code) });
    else if (!r.data.ok) setNotice({ kind: "error", text: `${m.model_id}: ${r.data.error}` });
    else setNotice({ kind: "ok", text: `${m.model_id} answered in ${r.data.latency_ms} ms${r.data.tools ? " and calls tools" : " but did not call the test tool (it can still chat)"}.` });
    onChanged();
  }

  async function remove() {
    const r = await api(path, { method: "DELETE" });
    if (r.ok) onChanged();
  }

  return (
    <tr className={m.available ? "" : "opacity-50"}>
      <td className="py-1 pr-3 font-mono">{m.model_id}</td>
      <td className="pr-3">
        {m.test_ok === null ? (
          <span className="text-fg-muted">not tested</span>
        ) : m.test_ok ? (
          <span className="text-ok">ok{m.tools ? " · tools" : " · no tools"}</span>
        ) : (
          <span className="text-crit" title={m.test_error ?? ""}>
            failed
          </span>
        )}
      </td>
      <td className="pr-3 text-fg-muted">{m.context_window ? `${Math.round(m.context_window / 1000)}k` : "—"}</td>
      <td className="pr-3 text-fg-muted">{m.available ? "" : "not listed"}</td>
      <td className="space-x-1 text-right">
        <Button disabled={busy} onClick={() => void test()}>
          Test
        </Button>
        {m.manual && <Button onClick={() => void remove()}>Remove</Button>}
      </td>
    </tr>
  );
}

function ProviderItem({ p, onChanged }: { p: AiProvider; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [newModel, setNewModel] = useState("");
  const [filter, setFilter] = useState("");

  async function act(path: string, method: string, ok: string, body?: unknown) {
    setBusy(true);
    setNotice(null);
    const r = await api<Record<string, unknown>>(path, { method, body });
    setBusy(false);
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    setNotice({ kind: "ok", text: ok });
    onChanged();
  }

  async function testConnection() {
    setBusy(true);
    setNotice(null);
    const r = await api<{ models: number }>(`/ai/providers/${p.id}/test`, { method: "POST" });
    setBusy(false);
    setNotice(r.ok ? { kind: "ok", text: `Connection works: ${r.data.models} models listed.` } : { kind: "error", text: message(r.code) });
  }

  const shown = p.models.filter((m) => !filter || m.model_id.toLowerCase().includes(filter.toLowerCase()));
  return (
    <li className="rounded border border-line p-3">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="font-mono text-sm font-semibold" onClick={() => setOpen(!open)}>
          {open ? "▾" : "▸"} {p.slug}
        </button>
        <span className="text-xs text-fg-muted">{p.type}</span>
        {!p.enabled && <span className="rounded bg-warn/20 px-1 text-xs text-warn">disabled</span>}
        {p.tls_unverified && <span className="rounded bg-crit/20 px-1 text-xs text-crit">TLS not verified</span>}
        <span className="text-xs text-fg-muted">
          {p.keys.length} key{p.keys.length === 1 ? "" : "s"} · {p.models.length} models ({p.models.filter((m) => m.test_ok).length} tested)
        </span>
        <span className="ml-auto flex flex-wrap gap-1">
          <Button disabled={busy} onClick={() => void testConnection()}>
            Test connection
          </Button>
          <Button disabled={busy} onClick={() => void act(`/ai/providers/${p.id}/models/refresh`, "POST", "Model list refreshed.")}>
            Refresh models
          </Button>
          <Button disabled={busy} onClick={() => void act(`/ai/providers/${p.id}`, "PUT", p.enabled ? "Disabled." : "Enabled.", { enabled: !p.enabled })}>
            {p.enabled ? "Disable" : "Enable"}
          </Button>
          <Button
            variant="danger"
            disabled={busy}
            onClick={() => {
              if (window.confirm(`Delete ${p.slug}, its keys and models? It leaves every route.`)) void act(`/ai/providers/${p.id}`, "DELETE", "Deleted.");
            }}
          >
            Delete
          </Button>
        </span>
      </div>
      <p className="mt-1 break-all text-xs text-fg-muted">{p.base_url}</p>
      <div className="mt-2">
        <NoticeBar notice={notice} />
      </div>
      {open && (
        <div className="mt-3 space-y-4">
          <div>
            <p className="mb-1 text-xs font-medium">Keys</p>
            <ul className="space-y-1 text-xs">
              {p.keys.map((k) => (
                <li key={k.id} className="flex flex-wrap items-center gap-2">
                  <span className="font-mono">····{k.last4 || "????"}</span>
                  <span className="text-fg-muted">{k.label}</span>
                  <span className={k.status === "ok" ? "text-ok" : k.status === "invalid" ? "text-crit" : "text-warn"}>
                    {k.status === "ok" ? "ok" : k.status === "invalid" ? "rejected by the provider" : `on hold: ${k.reason}`}
                  </span>
                  <Button
                    onClick={() => {
                      const value = window.prompt("New value for this key (it is stored sealed and never shown again)");
                      if (value) void act(`/ai/providers/${p.id}/keys/${k.id}`, "PUT", "Key replaced.", { value });
                    }}
                  >
                    Replace
                  </Button>
                  <Button onClick={() => void act(`/ai/providers/${p.id}/keys/${k.id}`, "DELETE", "Key deleted.")}>Delete</Button>
                </li>
              ))}
              {p.keys.length === 0 && <li className="text-fg-muted">No key (fine for a local engine).</li>}
            </ul>
            <div className="mt-2 flex gap-2">
              <TextInput type="password" autoComplete="off" placeholder="Add another key (a second account to fall back on)" value={newKey} onChange={(e) => setNewKey(e.target.value)} />
              <Button
                disabled={busy || !newKey.trim()}
                onClick={() => {
                  void act(`/ai/providers/${p.id}/keys`, "POST", "Key added.", { value: newKey.trim() });
                  setNewKey("");
                }}
              >
                Add key
              </Button>
            </div>
          </div>
          <div>
            <div className="mb-1 flex items-center gap-2">
              <p className="text-xs font-medium">Models</p>
              <TextInput className="max-w-56" placeholder="Filter" value={filter} onChange={(e) => setFilter(e.target.value)} />
            </div>
            <div className="max-h-80 overflow-auto">
              <table className="w-full text-xs">
                <tbody>
                  {shown.map((m) => (
                    <ModelRow key={m.model_id} provider={p} m={m} onChanged={onChanged} setNotice={setNotice} />
                  ))}
                </tbody>
              </table>
              {shown.length === 0 && <p className="text-xs text-fg-muted">No model. Press “Refresh models” or add one by name.</p>}
            </div>
            <div className="mt-2 flex gap-2">
              <TextInput placeholder="Add a model by name (not in the list)" value={newModel} onChange={(e) => setNewModel(e.target.value)} />
              <Button
                disabled={busy || !newModel.trim()}
                onClick={() => {
                  void act(`/ai/providers/${p.id}/models`, "PUT", "Model added: press Test on it.", [{ model_id: newModel.trim() }]);
                  setNewModel("");
                }}
              >
                Add model
              </Button>
            </div>
            <p className="mt-1 text-xs text-fg-muted">A model can be put on a route only after it passes Test (one tiny request with a tool).</p>
          </div>
        </div>
      )}
    </li>
  );
}

function AddProvider({ presets, existing, onAdded }: { presets: Preset[]; existing: string[]; onAdded: () => void }) {
  const [type, setType] = useState("");
  const [slug, setSlug] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [account, setAccount] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const preset = presets.find((p) => p.type === type);

  function pick(t: string) {
    setType(t);
    const p = presets.find((x) => x.type === t);
    setSlug(p && !existing.includes(p.type) ? p.type : "");
    setBaseUrl(p?.base_url.includes("{account_id}") ? "" : (p?.base_url ?? ""));
    setNotice(null);
  }

  async function add() {
    if (!preset) return;
    setBusy(true);
    setNotice(null);
    const body: Record<string, unknown> = { type, slug: slug.trim() };
    if (baseUrl.trim()) body.base_url = baseUrl.trim();
    if (preset.config_fields.includes("account_id")) body.config = { account_id: account.trim() };
    if (key.trim()) body.api_key = key.trim();
    if (preset.paid) body.enabled = false;
    const r = await api("/ai/providers", { method: "POST", body });
    setBusy(false);
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    setKey("");
    setType("");
    setNotice({ kind: "ok", text: `Added. ${preset.paid ? "It is switched off because it is billed per use: enable it when you want it used. " : ""}Test a model, then put it on a route.` });
    onAdded();
  }

  return (
    <div className="space-y-2 rounded border border-line p-3">
      <p className="text-xs font-medium">Add a provider</p>
      <NoticeBar notice={notice} />
      <div className="grid gap-2 md:grid-cols-2">
        <Field label="Provider">
          <Select value={type} onChange={(e) => pick(e.target.value)}>
            <option value="">Choose…</option>
            {presets.map((p) => (
              <option key={p.type} value={p.type}>
                {p.label}
                {p.paid ? " (billed)" : ""}
              </option>
            ))}
          </Select>
        </Field>
        {preset && (
          <>
            <Field label="Name" hint="Lowercase letters, digits, - and _. Used as provider/model in routes.">
              <TextInput value={slug} onChange={(e) => setSlug(e.target.value)} />
            </Field>
            <Field label="Base URL" hint={preset.notes || undefined}>
              <TextInput value={baseUrl} placeholder={preset.base_url} onChange={(e) => setBaseUrl(e.target.value)} />
            </Field>
            {preset.config_fields.includes("account_id") && (
              <Field label="Account id">
                <TextInput value={account} onChange={(e) => setAccount(e.target.value)} />
              </Field>
            )}
            {preset.key && (
              <Field label="API key" hint="Sealed in the vault. The connection and the key are tried before saving.">
                <TextInput type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} />
              </Field>
            )}
          </>
        )}
      </div>
      {preset && (
        <Button variant="primary" disabled={busy || !slug.trim() || (preset.key && !key.trim())} onClick={() => void add()}>
          Add provider
        </Button>
      )}
    </div>
  );
}

export function AiProvidersCard() {
  const providers = useLoad<AiProvider[]>("/ai/providers");
  const presets = useLoad<Preset[]>("/ai/providers/presets");
  const [showAdd, setShowAdd] = useState(false);
  return (
    <Card
      title="AI providers"
      actions={<Button onClick={() => setShowAdd(!showAdd)}>{showAdd ? "Close" : "Add provider"}</Button>}
    >
      <div className="space-y-3 text-sm">
        <p className="text-xs text-fg-secondary">
          Every provider you have a key for, with its models. A provider can hold several keys (a second account takes over when the
          first hits its limit). Models are used through routes below. Until a route has a model, the single endpoint set above keeps answering.
        </p>
        {providers.error && <Alert>{providers.error}</Alert>}
        {showAdd && (
          <AddProvider
            presets={presets.data ?? []}
            existing={(providers.data ?? []).map((p) => p.slug)}
            onAdded={() => {
              providers.reload();
              setShowAdd(false);
            }}
          />
        )}
        <ul className="space-y-2">
          {(providers.data ?? []).map((p) => (
            <ProviderItem key={p.id} p={p} onChanged={providers.reload} />
          ))}
          {providers.data && providers.data.length === 0 && <li className="text-fg-muted">No provider yet.</li>}
        </ul>
      </div>
    </Card>
  );
}

function RouteItem({ route, choices, onChanged }: { route: AiRoute; choices: { provider: string; model: string }[]; onChanged: () => void }) {
  const [draft, setDraft] = useState(route.entries.map((e) => ({ provider: e.provider, model: e.model, key_id: e.key_id })));
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const changed = JSON.stringify(draft.map((e) => [e.provider, e.model])) !== JSON.stringify(route.entries.map((e) => [e.provider, e.model]));

  function move(i: number, d: number) {
    const next = [...draft];
    const j = i + d;
    if (j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j], next[i]];
    setDraft(next);
  }

  async function save() {
    setBusy(true);
    setNotice(null);
    const r = await api(`/ai/routes/${encodeURIComponent(route.name)}`, { method: "PUT", body: { entries: draft } });
    setBusy(false);
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    setNotice({ kind: "ok", text: "Saved." });
    onChanged();
  }

  async function test() {
    setBusy(true);
    setNotice(null);
    const r = await api<{ model: string | null; notices: string[] }>(`/ai/routes/${encodeURIComponent(route.name)}/test`, { method: "POST" });
    setBusy(false);
    if (!r.ok) return setNotice({ kind: "error", text: message(r.code) });
    setNotice({ kind: "ok", text: `Answered by ${r.data.model}.${r.data.notices.length ? " " + r.data.notices.join(" ") : ""}` });
  }

  async function remove() {
    if (!window.confirm(`Delete the route ${route.name}?`)) return;
    const r = await api(`/ai/routes/${encodeURIComponent(route.name)}`, { method: "DELETE" });
    if (r.ok) onChanged();
  }

  const used = new Set(draft.map((e) => `${e.provider}/${e.model}`));
  const free = choices.filter((c) => !used.has(`${c.provider}/${c.model}`));
  return (
    <li className="rounded border border-line p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-semibold">{route.name}</span>
        <span className="text-xs text-fg-muted">
          {route.name === "default" ? "Copilot, Operate and the code generator" : route.name === "review" ? "second opinion" : "custom: pick it in Operate"}
        </span>
        <span className="ml-auto flex gap-1">
          <Button disabled={busy || draft.length === 0 || changed} onClick={() => void test()}>
            Test route
          </Button>
          {!route.protected && (
            <Button variant="danger" onClick={() => void remove()}>
              Delete
            </Button>
          )}
        </span>
      </div>
      <ol className="mt-2 space-y-1 text-xs">
        {draft.map((e, i) => (
          <li key={`${e.provider}/${e.model}`} className="flex items-center gap-2">
            <span className="w-5 text-fg-muted">{i + 1}</span>
            <span className="font-mono">
              {e.provider} / {e.model}
            </span>
            {i === 0 ? <span className="text-fg-muted">first choice</span> : <span className="text-fg-muted">fallback</span>}
            <span className="ml-auto flex gap-1">
              <Button disabled={i === 0} onClick={() => move(i, -1)}>
                ↑
              </Button>
              <Button disabled={i === draft.length - 1} onClick={() => move(i, 1)}>
                ↓
              </Button>
              <Button onClick={() => setDraft(draft.filter((_, k) => k !== i))}>Remove</Button>
            </span>
          </li>
        ))}
        {draft.length === 0 && <li className="text-fg-muted">No model on this route yet.</li>}
      </ol>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Select
          className="max-w-80"
          value=""
          onChange={(e) => {
            const c = free[Number(e.target.value)];
            if (c) setDraft([...draft, { provider: c.provider, model: c.model, key_id: null }]);
          }}
        >
          <option value="">{free.length ? "Add a model…" : "No other tested model"}</option>
          {free.map((c, i) => (
            <option key={`${c.provider}/${c.model}`} value={i}>
              {c.provider} / {c.model}
            </option>
          ))}
        </Select>
        <Button variant="primary" disabled={busy || !changed} onClick={() => void save()}>
          Save route
        </Button>
      </div>
      <div className="mt-2">
        <NoticeBar notice={notice} />
      </div>
    </li>
  );
}

export function AiRoutesCard() {
  const routes = useLoad<AiRoute[]>("/ai/routes");
  const providers = useLoad<AiProvider[]>("/ai/providers");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  const choices = (providers.data ?? [])
    .filter((p) => p.enabled)
    .flatMap((p) => p.models.filter((m) => m.test_ok && m.kind === "chat" && m.available).map((m) => ({ provider: p.slug, model: m.model_id })));

  async function create() {
    setError(null);
    const r = await api(`/ai/routes/${encodeURIComponent(name.trim())}`, { method: "PUT", body: { entries: [] } });
    if (!r.ok) return setError(message(r.code));
    setName("");
    routes.reload();
  }

  return (
    <Card title="Routes and fallback">
      <div className="space-y-3 text-sm">
        <p className="text-xs text-fg-secondary">
          A route is an ordered list of models. A request goes to the first one that works; when it fails (limit reached, outage, timeout, bad key,
          model gone) the next one answers, and the failing one rests for a while. “default” serves the Copilot, Operate and the code generator;
          “review” gives the second opinion. Only models that passed Test can be added.
        </p>
        {routes.error && <Alert>{routes.error}</Alert>}
        {error && <Alert>{error}</Alert>}
        <ul className="space-y-2">
          {(routes.data ?? []).map((r) => (
            <RouteItem
              key={`${r.name}-${r.entries.map((e) => `${e.provider}/${e.model}`).join(",")}`}
              route={r}
              choices={choices}
              onChanged={() => {
                routes.reload();
                providers.reload();
              }}
            />
          ))}
        </ul>
        <div className="flex gap-2">
          <TextInput placeholder="New route name (e.g. fast, big)" value={name} onChange={(e) => setName(e.target.value)} />
          <Button disabled={!name.trim()} onClick={() => void create()}>
            Create route
          </Button>
        </div>
      </div>
    </Card>
  );
}

const USAGE_SORT: Getters<UsageRow> = {
  model: (u) => `${u.provider}/${u.model}`,
  calls: (u) => u.calls,
  ok: (u) => u.ok,
  fallbacks: (u) => u.fallbacks,
  tokens: (u) => u.tokens_in + u.tokens_out,
  latency: (u) => u.latency_ms,
};

export function AiHealthCard() {
  const health = useLoad<HealthRow[]>("/ai/llm/health");
  const usage = useLoad<UsageRow[]>("/ai/llm/usage?days=7");
  const usageRows = usage.data ?? [];
  const { sorted: usageSorted, sort: usageSort, toggle: usageToggle } = useSortedRows(usageRows, USAGE_SORT);
  return (
    <Card title="Provider health and usage" actions={<Button onClick={() => { health.reload(); usage.reload(); }}>Refresh</Button>}>
      <div className="space-y-4 text-xs">
        {(health.data ?? []).length === 0 && <p className="text-fg-muted">Nothing to show until a provider exists.</p>}
        <ul className="space-y-1">
          {(health.data ?? []).map((h) => (
            <li key={h.provider} className="flex flex-wrap items-baseline gap-2">
              <span className="w-28 font-mono">{h.provider}</span>
              {!h.enabled && <span className="text-warn">disabled</span>}
              {h.entries.length === 0 && <span className="text-fg-muted">not on any route</span>}
              {h.entries.map((e) => (
                <span key={`${e.route}-${e.model}`} className="flex items-center gap-1">
                  <span className={e.state === "ok" ? "text-ok" : e.state === "disabled" ? "text-crit" : "text-warn"}>●</span>
                  <span className="font-mono">
                    {e.route}: {e.model}
                  </span>
                  {e.state !== "ok" && <span className="text-fg-muted">{e.reason}</span>}
                </span>
              ))}
            </li>
          ))}
        </ul>
        {(usage.data ?? []).length > 0 && (
          <div>
            <p className="mb-1 font-medium">Last 7 days</p>
            <table className="w-full">
              <thead className="text-left text-fg-muted">
                <tr>
                  <SortTh label="Model" col="model" sort={usageSort} onSort={usageToggle} className="py-1 pr-3 font-medium" />
                  <SortTh label="Calls" col="calls" sort={usageSort} onSort={usageToggle} className="py-1 pr-3 font-medium" />
                  <SortTh label="OK" col="ok" sort={usageSort} onSort={usageToggle} className="py-1 pr-3 font-medium" />
                  <SortTh label="Fallbacks" col="fallbacks" sort={usageSort} onSort={usageToggle} className="py-1 pr-3 font-medium" />
                  <SortTh label="Tokens in / out" col="tokens" sort={usageSort} onSort={usageToggle} className="py-1 pr-3 font-medium" />
                  <SortTh label="Latency" col="latency" sort={usageSort} onSort={usageToggle} className="py-1 font-medium" />
                </tr>
              </thead>
              <tbody>
                {usageSorted.map((u) => (
                  <tr key={`${u.provider}/${u.model}`}>
                    <td className="font-mono">
                      {u.provider} / {u.model}
                    </td>
                    <td>{u.calls}</td>
                    <td>{u.ok}</td>
                    <td>{u.fallbacks}</td>
                    <td>
                      {u.tokens_in} / {u.tokens_out}
                    </td>
                    <td>{u.latency_ms} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Card>
  );
}
