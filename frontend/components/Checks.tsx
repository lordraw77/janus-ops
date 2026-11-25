"use client";

import { CalendarClock, Lightbulb, Pencil, Play, Plus, Sparkles, Trash2, X } from "lucide-react";
import { type FormEvent, useState } from "react";

import { Alert, Button, Card, Field, Modal, Select, StateBadge, TextInput } from "@/components/ui";
import { type OidEntry, SnmpOidCatalog, useOidCatalog } from "@/components/SnmpOids";
import { ago } from "@/lib/agents";
import { api, message, useResource } from "@/lib/api";
import type { LlmChoices } from "@/lib/aiProviders";
import { useAuth } from "@/lib/auth";
import {
  type Check,
  type CheckDef,
  type CheckKind,
  DOWNTIME_PRESETS,
  KIND_LABEL,
  type ProbeInfo,
  type Suggestion,
  describeCheck,
} from "@/lib/monitoring";

const num = (v: FormDataEntryValue | null): number | null => {
  const s = String(v ?? "").trim();
  return s === "" ? null : Number(s);
};
const str = (v: FormDataEntryValue | null): string | undefined => {
  const s = String(v ?? "").trim();
  return s === "" ? undefined : s;
};

type CheckDraft = CheckDef & { name: string };
type CheckDrafts = { checks: CheckDraft[]; notes: string[]; warnings: string[]; model: string | null };

/** "Describe what to watch": a configured model drafts checks for the node. The person reads them,
 * loads one into the form to edit it, or creates them as they are. */
function AiCheckAssist({ nodeId, onUse, onCreated }: { nodeId: string; onUse: (def: CheckDef) => void; onCreated: () => void }) {
  const { can } = useAuth();
  const choices = useResource<LlmChoices>("/ai/llm/choices");
  const [prompt, setPrompt] = useState("");
  const [spec, setSpec] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CheckDrafts | null>(null);
  if (!can("ai.use")) return null;

  async function generate() {
    setBusy(true);
    setError(null);
    setResult(null);
    const r = await api<CheckDrafts>("/checks/generate", { method: "POST", body: { node_id: nodeId, prompt, spec: spec || null } });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    setResult(r.data);
  }
  async function createAll() {
    if (!result?.checks.length) return;
    setBusy(true);
    setError(null);
    for (const c of result.checks) {
      const r = await api("/checks", { method: "POST", body: { ...c, node_id: nodeId, enabled: true } });
      if (!r.ok) {
        setBusy(false);
        return setError(`${c.name}: ${message(r.code)}`);
      }
    }
    setBusy(false);
    onCreated();
  }
  const routes = choices.data?.routes ?? [];
  const models = choices.data?.models ?? [];
  return (
    <div className="mb-3 space-y-2 rounded border border-line bg-surface p-3 text-xs">
      <div className="flex items-center gap-2 font-semibold">
        <Sparkles className="h-3.5 w-3.5 text-primary" aria-hidden /> Describe what to watch
      </div>
      <textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        rows={3}
        maxLength={2000}
        placeholder="e.g. check that celery is running and that the myapp log has no errors"
        aria-label="What to watch"
        className="w-full rounded border border-line bg-canvas p-2 outline-none focus:border-primary"
      />
      <div className="flex flex-wrap items-center gap-2">
        {choices.data?.configured && (
          <select value={spec} onChange={(e) => setSpec(e.target.value)} aria-label="Model" className="max-w-72 rounded border border-line bg-canvas px-2 py-1.5">
            <option value="">default route</option>
            {routes.length > 0 && (
              <optgroup label="Routes (with fallback)">
                {routes.map((r) => (
                  <option key={r.name} value={r.name}>
                    {r.name}
                  </option>
                ))}
              </optgroup>
            )}
            {models.length > 0 && (
              <optgroup label="One model">
                {models.map((m) => (
                  <option key={m.spec} value={m.spec}>
                    {m.spec}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        )}
        <Button onClick={() => void generate()} disabled={busy || !prompt.trim()}>
          <Sparkles className="h-3 w-3" aria-hidden /> {busy ? "Thinking…" : "Draft checks"}
        </Button>
        <span className="text-fg-muted">The model sees this node&apos;s inventory, running units, ports and metric names, never secrets.</span>
      </div>
      {error && <Alert>{error}</Alert>}
      {result && (
        <div className="space-y-2">
          <p className="text-fg-muted">
            Drafted by <span className="font-mono">{result.model ?? "the model"}</span>. Nothing is saved until you create them.
          </p>
          {result.warnings.map((w) => (
            <Alert key={w} kind="info">
              {w}
            </Alert>
          ))}
          {result.checks.length === 0 && <p className="text-fg-muted">No usable check came back: rephrase the request or see the notes.</p>}
          {result.checks.map((c, i) => (
            <div key={`${c.name}-${i}`} className="flex items-start gap-2 rounded border border-line bg-canvas px-2 py-1.5">
              <div className="min-w-0 flex-1">
                <div className="font-semibold">{c.name}</div>
                <div className="text-fg-muted">
                  {KIND_LABEL[c.kind]} · {describeCheck(c)}
                </div>
              </div>
              <Button onClick={() => onUse(c)} title="Load into the form below to edit it">
                Use in form
              </Button>
              <Button onClick={() => setResult({ ...result, checks: result.checks.filter((_, j) => j !== i) })} aria-label={`Drop ${c.name}`} title="Drop this draft">
                <X className="h-3 w-3" aria-hidden />
              </Button>
            </div>
          ))}
          {result.notes.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-5">
              {result.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          )}
          {result.checks.length > 0 && (
            <Button variant="primary" onClick={() => void createAll()} disabled={busy}>
              <Plus className="h-3 w-3" aria-hidden /> Create {result.checks.length === 1 ? "this check" : `all ${result.checks.length}`}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/** Create or edit a check; fields follow the kind. */
export function CheckEditor({
  nodeId,
  check,
  onClose,
  onSaved,
}: {
  nodeId: string;
  check: Check | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  // a draft written by the model fills the form like a check being edited; the form is remounted
  const [draft, setDraft] = useState<{ def: CheckDef; n: number } | null>(null);
  const src: CheckDef | null = check ?? draft?.def ?? null;
  const [kind, setKind] = useState<CheckKind>(check?.kind ?? "metric");
  const [error, setError] = useState<string | null>(null);
  const [logsSource, setLogsSource] = useState(String(check?.params?.source ?? "unit"));
  const p = (src?.params ?? {}) as Record<string, unknown>;
  const thresholds = kind === "metric" || kind === "tcp" || kind === "http" || kind === "snmp" || kind === "probe" || kind === "logs";
  // SNMP: the OID comes from the catalog (with an instance for table columns) or is typed
  const catalog = useOidCatalog();
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [picked, setPicked] = useState<OidEntry | null>(null);
  const [oidText, setOidText] = useState(String(p.oid ?? ""));
  const [instance, setInstance] = useState("1");
  // a probe is picked from the catalog; its arguments follow from it
  const probes = useResource<ProbeInfo[]>(kind === "probe" ? "/probes" : null);
  const [probe, setProbe] = useState(String(p.probe ?? "postgres.connections_used_pct"));
  const probeArgs = (p.args ?? {}) as Record<string, unknown>;

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    let params: Record<string, unknown> = {};
    if (kind === "metric") {
      const labels: Record<string, string> = {};
      for (const pair of String(f.get("labels") ?? "").split(",")) {
        const [k, v] = pair.split("=").map((x) => x.trim());
        if (k && v) labels[k] = v;
      }
      params = { metric: str(f.get("metric")), labels, window_s: num(f.get("window_s")) ?? 0 };
    } else if (kind === "systemd") params = { unit: str(f.get("unit")) };
    else if (kind === "service") params = { unit: str(f.get("unit")) };
    else if (kind === "fritzbox") params = { item: str(f.get("item")) };
    else if (kind === "process")
      params = { match: f.get("match") ?? "name", pattern: str(f.get("pattern")), min_count: num(f.get("min_count")) ?? 1 };
    else if (kind === "logs") {
      const words = String(f.get("words") ?? "").split(/[,;]+/).map((w) => w.trim()).filter(Boolean);
      params =
        f.get("source") === "file"
          ? { source: "file", path: str(f.get("path")), words, last_lines: num(f.get("last_lines")) ?? 200 }
          : { source: "unit", unit: str(f.get("unit")), words, since_min: num(f.get("since_min")) ?? 15 };
    }
    else if (kind === "probe") {
      const wanted = probes.data?.find((x) => x.name === probe);
      const args: Record<string, string | boolean> = {};
      for (const name of Object.keys(wanted?.args ?? {})) {
        const v = f.get(`arg_${name}`);
        if (name === "verify_tls") args[name] = v === "on";
        else if (v && String(v).trim()) args[name] = String(v).trim();
      }
      params = { probe, ...(Object.keys(args).length ? { args } : {}) };
    }
    else if (kind === "port") params = { port: num(f.get("port")), proto: f.get("proto") };
    else if (kind === "tcp") params = { port: num(f.get("port")), host: str(f.get("host")) };
    else if (kind === "backup") {
      const ids = String(f.get("ignore_vmids") ?? "").split(/[\s,;]+/).filter(Boolean).map(Number);
      if (ids.some((n) => !Number.isInteger(n) || n < 1)) return setError("Guest ids are numbers, separated by commas.");
      params = { max_age_h: num(f.get("max_age_h")) ?? undefined, ignore_vmids: ids };
    }
    else if (kind === "snmp")
      params = {
        oid: picked?.indexed ? `${picked.oid}.${instance.trim() || "1"}` : oidText.trim(),
        scale: num(f.get("scale")) ?? 1,
        timeout_s: num(f.get("timeout_s")) ?? 5,
      };
    else if (kind === "http")
      params = {
        url: str(f.get("url")),
        expect_status: str(f.get("expect_status")) ?? "200-399",
        contains: str(f.get("contains")),
        verify_tls: f.get("verify_tls") === "on",
      };
    const body: CheckDef = {
      node_id: nodeId,
      name: String(f.get("name") ?? "").trim(),
      kind,
      params,
      comparison: thresholds ? ((f.get("comparison") as "gt" | "lt") ?? "gt") : null,
      warn_threshold: thresholds ? num(f.get("warn")) : null,
      crit_threshold: thresholds ? num(f.get("crit")) : null,
      check_cycles: num(f.get("cycles")) ?? 1,
      interval_s: num(f.get("interval")) ?? 60,
      enabled: f.get("enabled") === "on",
    };
    if (body.warn_threshold === null && body.crit_threshold === null) body.comparison = null;
    const r = check
      ? await api(`/checks/${check.id}`, { method: "PUT", body })
      : await api("/checks", { method: "POST", body });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }

  return (
    <Modal title={check ? `Edit ${check.name}` : "New check"} onClose={onClose}>
      {!check && (
        <AiCheckAssist
          nodeId={nodeId}
          onUse={(def) => {
            setKind(def.kind);
            if (def.kind === "probe") setProbe(String(def.params.probe ?? probe));
            if (def.kind === "logs") setLogsSource(String(def.params.source ?? "unit"));
            setDraft((d) => ({ def, n: (d?.n ?? 0) + 1 }));
          }}
          onCreated={onSaved}
        />
      )}
      <form key={draft?.n ?? 0} onSubmit={(e) => void submit(e)} className="max-h-[75vh] space-y-3 overflow-y-auto pr-1">
        {error && <Alert>{error}</Alert>}
        <Field label="Kind">
          <Select value={kind} onChange={(e) => setKind(e.target.value as CheckKind)} disabled={!!check}>
            {(Object.keys(KIND_LABEL) as CheckKind[]).map((k) => (
              <option key={k} value={k}>
                {KIND_LABEL[k]}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Name">
          <TextInput name="name" required maxLength={256} defaultValue={src?.name} />
        </Field>
        {kind === "metric" && (
          <>
            <Field label="Metric" hint="e.g. cpu.usage_pct, mem.used_pct, fs.used_pct, load.1, disk.util_pct">
              <TextInput name="metric" required defaultValue={String(p.metric ?? "cpu.usage_pct")} />
            </Field>
            <Field label="Labels" hint="Optional, e.g. mount=/ — empty = all series, the worst one counts">
              <TextInput
                name="labels"
                defaultValue={Object.entries((p.labels as Record<string, string>) ?? {})
                  .map(([k, v]) => `${k}=${v}`)
                  .join(",")}
              />
            </Field>
            <Field label="Average over (seconds)" hint="0 = latest value">
              <TextInput name="window_s" type="number" min={0} max={3600} defaultValue={String(p.window_s ?? 0)} />
            </Field>
          </>
        )}
        {kind === "fritzbox" && (
          <Field label="Item" hint="one part of the FRITZ!Box reading, polled over TR-064 from the server">
            <select name="item" required defaultValue={String(p.item ?? "wan_link")} className="w-full rounded border border-line bg-canvas px-2 py-1.5 text-xs">
              <option value="wan_link">WAN link</option>
              <option value="wan_connection">WAN connection</option>
              <option value="uptime">Uptime</option>
              <option value="traffic">Traffic</option>
            </select>
          </Field>
        )}
        {kind === "probe" && (
          <>
            <Field label="Probe" hint={probes.data?.find((x) => x.name === probe)?.description}>
              <Select value={probe} onChange={(e) => setProbe(e.target.value)}>
                {(probes.data ?? [{ name: probe } as ProbeInfo]).map((x) => (
                  <option key={x.name} value={x.name}>
                    {x.name}
                    {x.unit ? ` (${x.unit})` : ""}
                  </option>
                ))}
              </Select>
            </Field>
            {"url" in (probes.data?.find((x) => x.name === probe)?.args ?? {}) && (
              <Field label="Status page URL" hint="Apache: /server-status?auto, readable by the Janus-Ops server">
                <TextInput name="arg_url" required defaultValue={String(probeArgs.url ?? "")} placeholder="http://host/server-status?auto" />
              </Field>
            )}
            {"storage" in (probes.data?.find((x) => x.name === probe)?.args ?? {}) && (
              <Field label="Storage" hint="Empty = the fullest one">
                <TextInput name="arg_storage" defaultValue={String(probeArgs.storage ?? "")} />
              </Field>
            )}
            {"service" in (probes.data?.find((x) => x.name === probe)?.args ?? {}) && (
              <Field label="Service" hint="Name of the node's service; empty = the first one">
                <TextInput name="arg_service" defaultValue={String(probeArgs.service ?? "")} />
              </Field>
            )}
            {probes.data?.find((x) => x.name === probe)?.requires && (
              <p className="text-[11px] text-fg-muted">
                Needs the {probes.data.find((x) => x.name === probe)?.requires}{" "}
                {["postgres", "fritzbox"].includes(probes.data.find((x) => x.name === probe)?.requires ?? "") ? "service" : "capability"} on this node.
              </p>
            )}
          </>
        )}
        {kind === "service" && (
          <Field label="Unit" hint="systemd unit checked with systemctl is-active, e.g. ser2net.service">
            <TextInput name="unit" required defaultValue={String(p.unit ?? "")} placeholder="ser2net.service" />
          </Field>
        )}
        {kind === "systemd" && (
          <Field label="Unit" hint="Empty = alert on any failed unit">
            <TextInput name="unit" defaultValue={String(p.unit ?? "")} placeholder="nginx.service" />
          </Field>
        )}
        {(kind === "port" || kind === "tcp") && (
          <div className="grid grid-cols-2 gap-3">
            <Field label="Port">
              <TextInput name="port" type="number" min={1} max={65535} required defaultValue={String(p.port ?? "")} />
            </Field>
            {kind === "port" ? (
              <Field label="Protocol">
                <Select name="proto" defaultValue={String(p.proto ?? "tcp")}>
                  <option>tcp</option>
                  <option>udp</option>
                </Select>
              </Field>
            ) : (
              <Field label="Host" hint="Empty = node address">
                <TextInput name="host" defaultValue={String(p.host ?? "")} />
              </Field>
            )}
          </div>
        )}
        {kind === "snmp" && (
          <>
            <Field label="OID" hint="Pick a name from the catalog, or type a numeric OID">
              <div className="flex gap-2">
                <Select
                  className="flex-1"
                  value={picked?.id ?? ""}
                  onChange={(e) => {
                    const entry = catalog.data?.find((o) => o.id === e.target.value) ?? null;
                    setPicked(entry);
                    if (entry) setOidText(entry.oid);
                  }}
                >
                  <option value="">Type an OID</option>
                  {(catalog.data ?? []).map((o) => (
                    <option key={o.id} value={o.id}>
                      {o.name}
                      {o.verified ? "" : " (not verified)"}
                    </option>
                  ))}
                </Select>
                <Button type="button" onClick={() => setCatalogOpen(true)}>
                  Catalog
                </Button>
              </div>
            </Field>
            {picked?.indexed ? (
              <Field label={`Instance: ${picked.oid}.<n>`} hint="Disk, pool or volume number, from 1">
                <TextInput inputMode="numeric" value={instance} onChange={(e) => setInstance(e.target.value)} placeholder="1" />
              </Field>
            ) : (
              <Field label="Numeric OID" hint="e.g. 1.3.6.1.2.1.1.7.0">
                <TextInput
                  required
                  value={oidText}
                  onChange={(e) => {
                    setOidText(e.target.value);
                    setPicked(null);
                  }}
                  placeholder="1.3.6.1.2.1.1.7.0"
                />
              </Field>
            )}
            <div className="grid grid-cols-2 gap-3">
            <Field label="Scale" hint="Multiplies the value before the thresholds">
              <TextInput name="scale" type="number" step="any" min={0} defaultValue={String(p.scale ?? 1)} />
            </Field>
            <Field label="Timeout (s)">
              <TextInput name="timeout_s" type="number" min={1} max={30} defaultValue={String(p.timeout_s ?? 5)} />
            </Field>
            </div>
          </>
        )}
        {kind === "backup" && (
          <>
            <Field label="Max backup age (hours, empty = per job schedule)">
              <TextInput name="max_age_h" type="number" min={1} max={2160} defaultValue={String(p.max_age_h ?? "")} />
            </Field>
            <Field label="Guests not backed up on purpose" hint="VM/CT ids separated by commas: left out of this check.">
              <TextInput name="ignore_vmids" placeholder="101, 205" defaultValue={Array.isArray(p.ignore_vmids) ? p.ignore_vmids.join(", ") : ""} />
            </Field>
          </>
        )}
        {kind === "process" && (
          <>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Match">
                <Select name="match" defaultValue={String(p.match ?? "name")}>
                  <option value="name">exact process name</option>
                  <option value="cmdline">text in the command line</option>
                </Select>
              </Field>
              <Field label="At least (processes)">
                <TextInput name="min_count" type="number" min={1} max={1000} defaultValue={String(p.min_count ?? 1)} />
              </Field>
            </div>
            <Field label="Name or text" hint="Name: as `ps -e` shows it (15 characters at most). Needs the agent with exec allowed.">
              <TextInput name="pattern" required maxLength={100} defaultValue={String(p.pattern ?? "")} />
            </Field>
          </>
        )}
        {kind === "logs" && (
          <>
            <Field label="Where" hint="Needs the agent with exec allowed (Linux). Critical when a line has one of the words.">
              <Select name="source" value={logsSource} onChange={(e) => setLogsSource(e.target.value)}>
                <option value="unit">journal of a systemd unit</option>
                <option value="file">file under /var/log</option>
              </Select>
            </Field>
            {logsSource === "file" ? (
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2">
                  <Field label="File">
                    <TextInput name="path" required placeholder="/var/log/app/app.log" defaultValue={String(p.path ?? "/var/log/")} />
                  </Field>
                </div>
                <Field label="Last lines">
                  <TextInput name="last_lines" type="number" min={10} max={2000} defaultValue={String(p.last_lines ?? 200)} />
                </Field>
              </div>
            ) : (
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2">
                  <Field label="Unit">
                    <TextInput name="unit" required placeholder="nginx.service" defaultValue={String(p.unit ?? "")} />
                  </Field>
                </div>
                <Field label="Last minutes">
                  <TextInput name="since_min" type="number" min={1} max={1440} defaultValue={String(p.since_min ?? 15)} />
                </Field>
              </div>
            )}
            <Field label="Words" hint="Comma separated, any case. A line with one of them counts.">
              <TextInput name="words" defaultValue={(Array.isArray(p.words) ? (p.words as string[]) : ["error", "fatal", "exception", "panic"]).join(", ")} />
            </Field>
          </>
        )}
        {kind === "http" && (
          <>
            <Field label="URL">
              <TextInput name="url" required type="url" defaultValue={String(p.url ?? "https://")} />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Expected status">
                <TextInput name="expect_status" defaultValue={String(p.expect_status ?? "200-399")} />
              </Field>
              <Field label="Body contains">
                <TextInput name="contains" defaultValue={String(p.contains ?? "")} />
              </Field>
            </div>
            <label className="flex items-center gap-2 text-xs text-fg-secondary">
              <input type="checkbox" name="verify_tls" defaultChecked={p.verify_tls !== false} /> Verify the TLS certificate
            </label>
          </>
        )}
        {thresholds && (
          <div className="grid grid-cols-3 gap-3">
            <Field label="Alert when">
              <Select name="comparison" defaultValue={src?.comparison ?? "gt"}>
                <option value="gt">above</option>
                <option value="lt">below</option>
              </Select>
            </Field>
            <Field label={kind === "metric" || kind === "probe" || kind === "snmp" ? "Warn" : kind === "logs" ? "Warn (lines)" : "Warn (ms)"}>
              <TextInput name="warn" type="number" step="any" defaultValue={src?.warn_threshold?.toString() ?? ""} />
            </Field>
            <Field label={kind === "metric" || kind === "probe" || kind === "snmp" ? "Crit" : kind === "logs" ? "Crit (lines)" : "Crit (ms)"}>
              <TextInput name="crit" type="number" step="any" defaultValue={src?.crit_threshold?.toString() ?? ""} />
            </Field>
          </div>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Interval (s)">
            <TextInput name="interval" type="number" min={10} max={86400} defaultValue={String(src?.interval_s ?? 60)} />
          </Field>
          <Field label="Cycles before alerting" hint="Consecutive bad results">
            <TextInput name="cycles" type="number" min={1} max={100} defaultValue={String(src?.check_cycles ?? 3)} />
          </Field>
        </div>
        <label className="flex items-center gap-2 text-xs text-fg-secondary">
          <input type="checkbox" name="enabled" defaultChecked={check?.enabled ?? true} /> Enabled
        </label>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary">
            Save
          </Button>
        </div>
      </form>
      {catalogOpen && <SnmpOidCatalog onClose={() => setCatalogOpen(false)} onChanged={catalog.reload} />}
    </Modal>
  );
}

/** Schedule a downtime for a node, a check or a group. */
export function DowntimeDialog({
  target,
  onClose,
  onSaved,
}: {
  target: { node_id?: string; check_id?: string; group_id?: string; label: string };
  onClose: () => void;
  onSaved: () => void;
}) {
  const [minutes, setMinutes] = useState(60);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const startRaw = String(f.get("starts_at") ?? "");
    const start = startRaw ? new Date(startRaw) : new Date();
    const end = new Date(start.getTime() + minutes * 60_000);
    const scope = { node_id: target.node_id, check_id: target.check_id, group_id: target.group_id };
    const r = await api("/downtimes", {
      method: "POST",
      body: {
        ...scope,
        starts_at: startRaw ? start.toISOString() : undefined,
        ends_at: end.toISOString(),
        reason: String(f.get("reason") ?? "").trim(),
      },
    });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }

  return (
    <Modal title={`Downtime for ${target.label}`} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <p className="text-xs text-fg-secondary">
          Problems are still recorded, but no notification is sent until the downtime ends.
        </p>
        {error && <Alert>{error}</Alert>}
        <Field label="Start" hint="Empty = now">
          <TextInput name="starts_at" type="datetime-local" />
        </Field>
        <Field label="Duration">
          <div className="flex flex-wrap gap-1">
            {DOWNTIME_PRESETS.map(([label, m]) => (
              <button
                key={m}
                type="button"
                onClick={() => setMinutes(m)}
                aria-pressed={minutes === m}
                className={`rounded border px-2 py-1 text-xs ${minutes === m ? "border-primary text-primary" : "border-line text-fg-secondary"}`}
              >
                {label}
              </button>
            ))}
            <TextInput
              type="number"
              min={5}
              aria-label="Minutes"
              value={minutes}
              onChange={(e) => setMinutes(Number(e.target.value))}
              className="!w-24"
            />
            <span className="self-center text-xs text-fg-muted">minutes</span>
          </div>
        </Field>
        <Field label="Reason">
          <TextInput name="reason" required maxLength={1000} placeholder="Kernel upgrade" />
        </Field>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary">
            Schedule
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function AckDialog({ label, problemId, onClose, onSaved }: { label: string; problemId: string; onClose: () => void; onSaved: () => void }) {
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const comment = String(new FormData(e.currentTarget).get("comment") ?? "").trim();
    const r = await api(`/problems/${problemId}/ack`, { method: "POST", body: { comment } });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }
  return (
    <Modal title={`Acknowledge ${label}`} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <p className="text-xs text-fg-secondary">
          No more notifications for this problem; a worse state clears the acknowledgement.
        </p>
        {error && <Alert>{error}</Alert>}
        <Field label="Comment">
          <TextInput name="comment" required maxLength={1000} autoFocus placeholder="Looking into it" />
        </Field>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary">
            Acknowledge
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function CloseProblemDialog({ label, problemId, onClose, onSaved }: { label: string; problemId: string; onClose: () => void; onSaved: () => void }) {
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const comment = String(new FormData(e.currentTarget).get("comment") ?? "").trim();
    const r = await api(`/problems/${problemId}/close`, { method: "POST", body: { comment } });
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }
  return (
    <Modal title={`Close ${label}`} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <p className="text-xs text-fg-secondary">
          The problem moves to the history now and the check runs again. If it still fails, a new problem opens (and
          notifies again): to silence a real problem use Acknowledge or a downtime instead.
        </p>
        {error && <Alert>{error}</Alert>}
        <Field label="Comment" hint="Optional, kept in the audit log.">
          <TextInput name="comment" maxLength={1000} autoFocus placeholder="Fixed by hand / obsolete" />
        </Field>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary">
            Close problem
          </Button>
        </div>
      </form>
    </Modal>
  );
}

type AttentionGuest = {
  vmid: number;
  name: string | null;
  type: string | null;
  node: string | null;
  status: string;
  last_backup: number | null;
  ignored: boolean;
};

/** Pick the guests that are not backed up on purpose: the backup check leaves them out and runs
 * again, so the problem closes by itself when nothing else is wrong. */
export function IgnoreBackupGuestsDialog({ label, checkId, onClose, onSaved }: { label: string; checkId: string; onClose: () => void; onSaved: () => void }) {
  const res = useResource<{ guests: AttentionGuest[]; ignored: number[] }>(`/checks/${checkId}/backup-attention`);
  const [picked, setPicked] = useState<Set<number> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const guests = res.data?.guests ?? [];
  // by default the guests already ignored stay ignored; nothing new is picked for the person
  const chosen = picked ?? new Set(guests.filter((g) => g.ignored).map((g) => g.vmid));
  // ignored ids of guests that no longer exist, or are fine now, are kept as they are
  const hidden = (res.data?.ignored ?? []).filter((v) => !guests.some((g) => g.vmid === v));
  function toggle(v: number) {
    const next = new Set(chosen);
    if (next.has(v)) next.delete(v);
    else next.add(v);
    setPicked(next);
  }
  async function save() {
    setBusy(true);
    const r = await api(`/checks/${checkId}/backup-ignore`, { method: "PUT", body: { vmids: [...chosen, ...hidden] } });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }
  return (
    <Modal title={`Guests not backed up: ${label}`} onClose={onClose}>
      <div className="space-y-3 text-xs">
        <p className="text-fg-secondary">
          Tick the guests you do not back up on purpose. The check leaves them out from now on and runs again; you can
          undo this here or in the check&apos;s settings.
        </p>
        {res.error && <Alert>{res.error}</Alert>}
        {error && <Alert>{error}</Alert>}
        {!res.data && !res.error && <p className="py-4 text-center text-fg-muted">Asking Proxmox…</p>}
        {res.data && guests.length === 0 && <p className="text-fg-muted">Every guest has a recent backup.</p>}
        {guests.map((g) => (
          <label key={g.vmid} className="flex items-center gap-2 rounded border border-line px-2 py-1.5">
            <input type="checkbox" checked={chosen.has(g.vmid)} onChange={() => toggle(g.vmid)} />
            <span className="font-mono">{g.vmid}</span>
            <span className="font-semibold">{g.name ?? "?"}</span>
            <span className="text-fg-muted">
              {g.type} on {g.node}
            </span>
            <span className="ml-auto text-fg-muted">
              {g.ignored ? "ignored · " : ""}
              {g.status}
              {g.last_backup ? ` · last ${new Date(g.last_backup * 1000).toLocaleDateString()}` : " · never"}
            </span>
          </label>
        ))}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={() => void save()} disabled={busy || !res.data}>
            Save
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export function BulkProblemsDialog({ action, ids, onClose, onDone }: { action: "ack" | "close"; ids: string[]; onClose: () => void; onDone: (r: { done: number; skipped: number }) => void }) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ack = action === "ack";
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const comment = String(new FormData(e.currentTarget).get("comment") ?? "").trim();
    setBusy(true);
    const r = await api<{ done: number; skipped: number }>("/problems/bulk", { method: "POST", body: { ids, action, comment } });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    onDone(r.data);
  }
  return (
    <Modal title={`${ack ? "Acknowledge" : "Close"} ${ids.length} problem${ids.length === 1 ? "" : "s"}`} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <p className="text-xs text-fg-secondary">
          {ack
            ? "No more notifications for these problems; a worse state clears the acknowledgement."
            : "The problems move to the history and their checks run again. Those that still fail open a new problem (and notify again): to silence real problems use Acknowledge or a downtime."}
        </p>
        {error && <Alert>{error}</Alert>}
        <Field label="Comment" hint={ack ? undefined : "Optional, kept in the audit log."}>
          <TextInput name="comment" required={ack} maxLength={1000} autoFocus placeholder={ack ? "Looking into it" : "Fixed by hand / obsolete"} />
        </Field>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy}>
            {ack ? "Acknowledge" : "Close"} {ids.length}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function Flags({ c }: { c: { is_flapping: boolean; unreachable: boolean; in_downtime: boolean; acknowledged_at?: string | null } }) {
  const flags = [
    c.acknowledged_at && ["ACK", "border-telemetry/40 text-telemetry"],
    c.in_downtime && ["DOWNTIME", "border-telemetry/40 text-telemetry"],
    c.unreachable && ["UNREACHABLE", "border-warn/40 text-warn"],
    c.is_flapping && ["FLAPPING", "border-unknown/40 text-unknown"],
  ].filter(Boolean) as [string, string][];
  return (
    <>
      {flags.map(([label, cls]) => (
        <span key={label} className={`ml-1 rounded border px-1 font-mono text-[9px] ${cls}`}>
          {label}
        </span>
      ))}
    </>
  );
}

/** The checks of one node, discovery suggestions and node downtime. */
export function ChecksCard({ nodeId, nodeName }: { nodeId: string; nodeName: string }) {
  const { can } = useAuth();
  const manage = can("monitoring.manage");
  const checks = useResource<Check[]>(`/checks?node_id=${nodeId}`);
  const suggestions = useResource<Suggestion[]>(`/nodes/${nodeId}/discovery`);
  const [editing, setEditing] = useState<Check | "new" | null>(null);
  const [downtime, setDowntime] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = () => {
    checks.reload();
    suggestions.reload();
  };

  async function add(list: Suggestion[]) {
    for (const s of list) {
      const r = await api("/checks", { method: "POST", body: s.check });
      if (!r.ok) {
        setError(message(r.code));
        break;
      }
    }
    reload();
  }

  async function remove(c: Check) {
    if (!window.confirm(`Delete the check "${c.name}" and its history?`)) return;
    const r = await api(`/checks/${c.id}`, { method: "DELETE" });
    setError(r.ok ? null : message(r.code));
    reload();
  }

  async function runNow(c: Check) {
    await api(`/checks/${c.id}/run`, { method: "POST" });
    window.setTimeout(checks.reload, 1500);
  }

  return (
    <Card
      title="Checks"
      actions={
        manage && (
          <div className="flex gap-2">
            <Button onClick={() => setDowntime(true)}>
              <CalendarClock className="h-3 w-3" aria-hidden /> Downtime
            </Button>
            <Button variant="primary" onClick={() => setEditing("new")}>
              <Plus className="h-3 w-3" aria-hidden /> New check
            </Button>
          </div>
        )
      }
    >
      {(error || checks.error) && <Alert>{error ?? checks.error}</Alert>}
      {checks.data?.length === 0 && <p className="text-sm text-fg-muted">No checks yet.</p>}
      <div className="divide-y divide-line">
        {(checks.data ?? []).map((c) => (
          <div key={c.id} className={`flex items-start gap-3 py-2 ${c.enabled ? "" : "opacity-50"}`}>
            <StateBadge state={c.enabled ? c.state : null} soft={c.state_type === "soft"} />
            <div className="min-w-0 flex-1">
              <div className="text-sm">
                {c.name}
                <Flags c={c} />
                {!c.enabled && <span className="ml-2 text-[10px] text-fg-muted">disabled</span>}
              </div>
              <div className="truncate font-mono text-[11px] text-fg-muted">{describeCheck(c)}</div>
              {c.last_output && <div className="truncate text-xs text-fg-secondary">{c.last_output}</div>}
            </div>
            <div className="hidden shrink-0 text-right font-mono text-[10px] text-fg-muted sm:block">
              <div>every {c.interval_s}s × {c.check_cycles}</div>
              <div>{c.last_checked_at ? ago(c.last_checked_at) : "not run yet"}</div>
            </div>
            {manage && (
              <div className="flex shrink-0 gap-1">
                <Button onClick={() => void runNow(c)} aria-label={`Run ${c.name} now`} title="Run now">
                  <Play className="h-3 w-3" aria-hidden />
                </Button>
                <Button onClick={() => setEditing(c)} aria-label={`Edit ${c.name}`}>
                  <Pencil className="h-3 w-3" aria-hidden />
                </Button>
                <Button variant="danger" onClick={() => void remove(c)} aria-label={`Delete ${c.name}`}>
                  <Trash2 className="h-3 w-3" aria-hidden />
                </Button>
              </div>
            )}
          </div>
        ))}
      </div>
      {manage && (suggestions.data?.length ?? 0) > 0 && (
        <div className="mt-3 rounded border border-dashed border-line p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="flex items-center gap-1.5 text-xs font-semibold text-fg-secondary">
              <Lightbulb className="h-3.5 w-3.5 text-warn" aria-hidden /> Suggested checks
            </span>
            <Button onClick={() => void add(suggestions.data ?? [])}>Add all</Button>
          </div>
          <ul className="space-y-1">
            {suggestions.data!.map((s) => (
              <li key={s.check.name} className="flex items-center gap-2 text-xs">
                <span className="min-w-0 flex-1 truncate">
                  {s.check.name}
                  <span className="ml-2 font-mono text-[10px] text-fg-muted">{describeCheck(s.check)}</span>
                </span>
                <Button onClick={() => void add([s])} aria-label={`Add ${s.check.name}`}>
                  <Plus className="h-3 w-3" aria-hidden />
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {editing && (
        <CheckEditor
          nodeId={nodeId}
          check={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
      {downtime && (
        <DowntimeDialog
          target={{ node_id: nodeId, label: nodeName }}
          onClose={() => setDowntime(false)}
          onSaved={() => {
            setDowntime(false);
            reload();
          }}
        />
      )}
    </Card>
  );
}
