"use client";

import { Download, FolderOpen, RotateCw, ShieldOff, Terminal, Trash2 } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type FormEvent, Suspense, useEffect, useState } from "react";

import { ChecksCard } from "@/components/Checks";
import { LineChart, type LineSeries } from "@/components/LineChart";
import { TemplateSuggestions } from "@/components/TemplateSuggestions";
import { Alert, Button, Card, Field, Modal, TextInput } from "@/components/ui";
import {
  AGENT_STATUS,
  type AgentDetail,
  type Chart,
  type Series,
  ago,
  formatBytes,
  formatDuration,
} from "@/lib/agents";
import { api, message, useResource } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { EnvBadge } from "@/components/EnvBadge";

const RANGES = ["1h", "6h", "24h", "7d", "30d", "90d"] as const;
type Range = (typeof RANGES)[number];
const CHART_POLL_MS = 30_000;

type ChartSpec = {
  title: string;
  names: string[];
  fixedMax?: number;
  label: (s: Series) => string;
  /** many devices/interfaces: keep the busiest ones (by peak) instead of the first ones */
  busiest?: boolean;
};

const NAMES: Record<string, string> = {
  "cpu.usage_pct": "busy",
  "cpu.iowait_pct": "I/O wait",
  "cpu.steal_pct": "steal",
  "mem.used_pct": "RAM",
  "swap.used_pct": "swap",
  "load.1": "1 min",
  "load.5": "5 min",
  "load.15": "15 min",
  "disk.read_bytes_ps": "read",
  "disk.write_bytes_ps": "write",
  "net.rx_bytes_ps": "in",
  "net.tx_bytes_ps": "out",
};

const byLabel = (key: string) => (s: Series) => s.labels[key] ?? s.name;

const CHARTS: ChartSpec[] = [
  { title: "CPU", names: ["cpu.usage_pct", "cpu.iowait_pct", "cpu.steal_pct"], fixedMax: 100, label: (s) => NAMES[s.name] },
  { title: "Memory", names: ["mem.used_pct", "swap.used_pct"], fixedMax: 100, label: (s) => NAMES[s.name] },
  { title: "Load average", names: ["load.1", "load.5", "load.15"], label: (s) => NAMES[s.name] },
  { title: "File systems used", names: ["fs.used_pct"], fixedMax: 100, label: (s) => s.labels.mount ?? "" },
  { title: "Disk read", names: ["disk.read_bytes_ps"], label: byLabel("device"), busiest: true },
  { title: "Disk write", names: ["disk.write_bytes_ps"], label: byLabel("device"), busiest: true },
  { title: "Network in", names: ["net.rx_bytes_ps"], label: byLabel("iface"), busiest: true },
  { title: "Network out", names: ["net.tx_bytes_ps"], label: byLabel("iface"), busiest: true },
];

const peak = (s: Series) => Math.max(0, ...s.points.map((p) => p.max));

/** Series of one chart in a stable order: catalogue order, then label (or busiest first). */
function seriesFor(spec: ChartSpec, all: Series[]): Series[] {
  const mine = all.filter((s) => spec.names.includes(s.name));
  return mine.sort((a, b) =>
    spec.busiest
      ? peak(b) - peak(a) || spec.label(a).localeCompare(spec.label(b))
      : spec.names.indexOf(a.name) - spec.names.indexOf(b.name) ||
        spec.label(a).localeCompare(spec.label(b)),
  );
}

function Charts({ nodeId }: { nodeId: string }) {
  const [range, setRange] = useState<Range>("1h");
  const [chart, setChart] = useState<{ data: Chart | null; error: string | null }>({ data: null, error: null });

  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams({ range });
    for (const c of CHARTS) for (const n of c.names) params.append("name", n);
    const load = () =>
      void api<Chart>(`/telemetry/nodes/${nodeId}?${params}`).then((r) => {
        if (!cancelled) setChart(r.ok ? { data: r.data, error: null } : { data: null, error: message(r.code) });
      });
    load();
    const timer = setInterval(load, CHART_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [nodeId, range]);

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-fg-muted">Telemetry</h2>
        <div className="flex gap-1" role="group" aria-label="Time range">
          {RANGES.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => setRange(r)}
              aria-pressed={range === r}
              className={`rounded border px-2 py-0.5 font-mono text-[11px] ${range === r ? "border-primary text-primary" : "border-line text-fg-muted hover:text-fg"}`}
            >
              {r}
            </button>
          ))}
        </div>
      </div>
      {chart.error && <Alert>{chart.error}</Alert>}
      {chart.data && (
        <div className="grid gap-3 lg:grid-cols-2">
          {CHARTS.map((spec) => {
            const series = seriesFor(spec, chart.data!.series);
            const lines: LineSeries[] = series.map((s) => ({
              key: `${s.name}${JSON.stringify(s.labels)}`,
              label: spec.label(s),
              points: s.points,
            }));
            return (
              <LineChart
                key={spec.title}
                title={spec.title}
                series={lines}
                unit={series[0]?.unit ?? (spec.fixedMax ? "%" : null)}
                bucketS={chart.data!.bucket_s}
                fixedMax={spec.fixedMax}
              />
            );
          })}
        </div>
      )}
    </section>
  );
}

function InstallDialog({ agent, onClose, onDone }: { agent: AgentDetail; onClose: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const proxy = String(form.get("proxy") ?? "").trim();
    const allowDocker = form.get("allow_docker") === "on";
    const allowExec = form.get("allow_exec") === "on";
    const allowPty = form.get("allow_pty") === "on";
    setBusy(true);
    setError(null);
    const r = await api(`/agents/${agent.node_id}/bootstrap`, {
      method: "POST",
      body: { proxy: proxy || null, allow_docker: allowDocker, allow_exec: allowExec, allow_pty: allowPty },
    });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    onDone();
  }

  const reinstall = agent.status !== "not_installed";
  return (
    <Modal title={reinstall ? `Reinstall the agent on ${agent.node_name}` : `Install the agent on ${agent.node_name}`} onClose={onClose}>
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <p className="text-xs text-fg-secondary">
          Janus-Ops connects over SSH with the node&apos;s credential (root or passwordless sudo), copies the
          agent to <code className="font-mono">/usr/local/bin/janus-agent</code>, writes its configuration with a
          one-time enrollment token and starts the <code className="font-mono">janus-agent</code> systemd
          service. The agent only connects out to this server over HTTPS; it opens no port.
          {reinstall && " Reinstalling also upgrades it to this server's version; its key is kept."}
        </p>
        {error && <Alert>{error}</Alert>}
        <Field label="HTTP(S) proxy" hint="Optional, e.g. http://proxy.example:3128. Empty = direct (or HTTPS_PROXY of the service).">
          <TextInput name="proxy" maxLength={512} placeholder="http://proxy:3128" />
        </Field>
        <label className="flex items-start gap-2 text-xs text-fg-secondary">
          <input type="checkbox" name="allow_docker" defaultChecked={agent.policy?.allow_docker ?? false} className="mt-0.5" />
          <span>
            Allow Docker: the agent relays the Docker Engine API (containers, logs, exec). This is root-equivalent
            access to the node; it is written in the node&apos;s local policy, which the server cannot change later.
          </span>
        </label>
        <label className="flex items-start gap-2 text-xs text-fg-secondary">
          <input type="checkbox" name="allow_exec" defaultChecked={agent.policy?.allow_exec ?? false} className="mt-0.5" />
          <span>
            Allow commands: the agent runs the commands of action plans approved in the AI Copilot, without a
            shell, as root. Written in the node&apos;s local policy; the server cannot change it later.
          </span>
        </label>
        <label className="flex items-start gap-2 text-xs text-fg-secondary">
          <input type="checkbox" name="allow_pty" defaultChecked={agent.policy?.allow_pty ?? false} className="mt-0.5" />
          <span>
            Allow shell: an interactive shell (as the agent&apos;s user, root) in the web terminal through the
            agent, without SSH. Written in the node&apos;s local policy; the server cannot change it later.
          </span>
        </label>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? "Installing…" : reinstall ? "Reinstall" : "Install"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function WindowsInstallDialog({ agent, onClose }: { agent: AgentDetail; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ command: string; expires_in_s: number } | null>(null);
  const [copied, setCopied] = useState(false);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const proxy = String(form.get("proxy") ?? "").trim();
    setBusy(true);
    setError(null);
    const r = await api<{ command: string; expires_in_s: number }>(`/agents/${agent.node_id}/install-command`, {
      method: "POST",
      body: { proxy: proxy || null, allow_exec: form.get("allow_exec") === "on", allow_docker: false },
    });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    setResult(r.data);
  }

  async function copy() {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(result.command);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <Modal title={`Install the agent on ${agent.node_name} (Windows)`} onClose={onClose}>
      {!result ? (
        <form onSubmit={(e) => void submit(e)} className="space-y-3">
          <p className="text-xs text-fg-secondary">
            No SSH needed. You get a one-line PowerShell command to run on the node, in a PowerShell opened as
            administrator. It downloads the agent from this server (checked against a SHA-256 and, with a private
            CA, a pinned certificate), writes its configuration with a one-time enrollment token and installs the{" "}
            <code className="font-mono">JanusAgent</code> Windows service. The agent only connects out to this
            server over HTTPS; it opens no port.
          </p>
          {error && <Alert>{error}</Alert>}
          <Field label="HTTP(S) proxy" hint="Optional, e.g. http://proxy.example:3128. Empty = direct.">
            <TextInput name="proxy" maxLength={512} placeholder="http://proxy:3128" />
          </Field>
          <label className="flex items-start gap-2 text-xs text-fg-secondary">
            <input type="checkbox" name="allow_exec" defaultChecked={agent.policy?.allow_exec ?? false} className="mt-0.5" />
            <span>
              Allow commands: the agent runs the commands of approved action plans and workflows, without a shell,
              as SYSTEM. Written in the node&apos;s local policy; the server cannot change it later.
            </span>
          </label>
          <p className="text-xs text-fg-muted">Not available on Windows yet: interactive shell, Docker, signed self-update.</p>
          <div className="flex justify-end gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={busy}>
              {busy ? "Generating…" : "Generate command"}
            </Button>
          </div>
        </form>
      ) : (
        <div className="space-y-3">
          <p className="text-xs text-fg-secondary">
            Run this in PowerShell as administrator on <strong>{agent.node_name}</strong>. It is valid for{" "}
            {Math.round(result.expires_in_s / 60)} minutes and works once; it contains the enrollment token, so do not
            share it.
          </p>
          <textarea
            readOnly
            value={result.command}
            rows={6}
            onFocus={(e) => e.currentTarget.select()}
            className="w-full rounded border border-line bg-elevated p-2 font-mono text-[11px]"
            aria-label="PowerShell command"
          />
          <div className="flex justify-end gap-2">
            <Button onClick={() => void copy()}>{copied ? "Copied" : "Copy"}</Button>
            <Button variant="primary" onClick={onClose}>
              Done
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}

function AgentCard({ agent, reload }: { agent: AgentDetail; reload: () => void }) {
  const { can } = useAuth();
  const [installing, setInstalling] = useState(false);
  const [installingWindows, setInstallingWindows] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const status = AGENT_STATUS[agent.status];
  const manage = can("agents.manage");

  async function updateAgent() {
    if (!window.confirm(`Push the signed agent update to ${agent.node_name}? The agent verifies the signature, replaces itself and restarts.`)) return;
    setNotice(null);
    const r = await api<{ from_version: string | null; to_version: string }>(`/agents/${agent.node_id}/update`, { method: "POST" });
    setNotice(r.ok ? { kind: "ok", text: `Updated to ${r.data.to_version}; the agent is restarting.` } : { kind: "error", text: message(r.code) });
    if (r.ok) setTimeout(reload, 4000);
  }

  async function act(action: "revoke" | "uninstall", confirmText: string) {
    if (!window.confirm(confirmText)) return;
    const r = await api(`/agents/${agent.node_id}/${action}`, { method: "POST" });
    setNotice(r.ok ? { kind: "ok", text: action === "revoke" ? "Agent revoked." : "Agent removed from the node." } : { kind: "error", text: message(r.code) });
    reload();
  }

  const rows: [string, string | null][] = [
    ["Version", agent.version ? `${agent.version}${agent.outdated ? " (outdated: reinstall to upgrade)" : ""}` : null],
    ["System", [agent.os, agent.arch].filter(Boolean).join(" · ") || null],
    ["Kernel", agent.kernel],
    ["Host name", agent.hostname],
    ["Key", agent.key_fingerprint],
    ["Connected", agent.connected_since ? `since ${new Date(agent.connected_since).toLocaleString()} from ${agent.remote_ip}` : null],
    ["Last seen", agent.last_seen_at ? ago(agent.last_seen_at) : null],
    ["Interval", agent.interval_s ? `${agent.interval_s} s` : null],
    [
      "Local policy",
      agent.policy
        ? (["allow_exec", "allow_pty", "allow_docker"] as const).map((k) => `${k.slice(6)} ${agent.policy![k] ? "on" : "off"}`).join(" · ")
        : null,
    ],
  ];

  return (
    <Card
      title="Janus agent"
      actions={
        <span className={`rounded border px-1.5 py-0.5 text-[11px] ${status.className}`}>{status.label}</span>
      }
    >
      {notice && <Alert kind={notice.kind}>{notice.text}</Alert>}
      {!agent.capability ? (
        <p className="text-sm text-fg-muted">
          Enable the <em>Janus agent</em> capability in the <Link href="/inventory/" className="text-primary">inventory</Link> to
          monitor this node.
        </p>
      ) : (
        <>
          <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-xs">
            {rows
              .filter(([, v]) => v)
              .map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-fg-muted">{k}</dt>
                  <dd className="break-all font-mono text-fg-secondary">{v}</dd>
                </div>
              ))}
          </dl>
          {agent.status === "not_installed" && <p className="text-xs text-fg-muted">Not installed yet.</p>}
          {manage && (
            <div className="mt-3 flex flex-wrap gap-2">
              {agent.has_ssh ? (
                <Button variant="primary" onClick={() => setInstalling(true)}>
                  {agent.status === "not_installed" ? <Download className="h-3 w-3" aria-hidden /> : <RotateCw className="h-3 w-3" aria-hidden />}
                  {agent.status === "not_installed" ? "Install via SSH" : "Reinstall / upgrade"}
                </Button>
              ) : (
                <span className="text-xs text-fg-muted">Add an SSH capability to install the agent from here.</span>
              )}
              <Button onClick={() => setInstallingWindows(true)} title="No SSH: a PowerShell command to run on the Windows node">
                <Download className="h-3 w-3" aria-hidden /> Install on Windows
              </Button>
              {agent.status === "online" && !(agent.os ?? "").startsWith("Windows") && (
                <Button onClick={() => void updateAgent()} title="Signed self-update: no SSH needed, the agent must hold the release public key">
                  <Download className="h-3 w-3" aria-hidden /> Update (signed)
                </Button>
              )}
              {agent.enrolled_at && !agent.revoked_at && (
                <Button
                  onClick={() =>
                    void act("revoke", `Revoke the agent of ${agent.node_name}? Its key is refused until you reinstall it.`)
                  }
                >
                  <ShieldOff className="h-3 w-3" aria-hidden /> Revoke
                </Button>
              )}
              {agent.has_ssh && agent.status !== "not_installed" && (
                <Button
                  variant="danger"
                  onClick={() =>
                    void act("uninstall", `Stop and remove the agent from ${agent.node_name} over SSH? Collected metrics are kept.`)
                  }
                >
                  <Trash2 className="h-3 w-3" aria-hidden /> Uninstall
                </Button>
              )}
            </div>
          )}
        </>
      )}
      {installingWindows && <WindowsInstallDialog agent={agent} onClose={() => { setInstallingWindows(false); reload(); }} />}
      {installing && (
        <InstallDialog
          agent={agent}
          onClose={() => setInstalling(false)}
          onDone={() => {
            setInstalling(false);
            setNotice({ kind: "ok", text: "Agent installed." });
            reload();
          }}
        />
      )}
    </Card>
  );
}

function Facts({ agent }: { agent: AgentDetail }) {
  const f = agent.facts;
  if (!f) return null;
  return (
    <div className="grid gap-3 lg:grid-cols-3">
      <Card title="Top processes (CPU over the last minute)">
        <table className="w-full table-fixed text-xs">
          <colgroup>
            <col />
            <col className="w-14" />
            <col className="w-20" />
          </colgroup>
          <thead className="text-left text-[10px] uppercase text-fg-muted">
            <tr>
              <th className="font-medium">Process</th>
              <th className="text-right font-medium">CPU</th>
              <th className="text-right font-medium">RSS</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {[...f.top_cpu, ...f.top_mem.filter((m) => !f.top_cpu.some((c) => c.pid === m.pid))].map((p) => (
              <tr key={p.pid} className="align-top">
                <td className="max-w-0 py-0.5 pr-2" title={`pid ${p.pid}${p.cmd ? `\n${p.cmd}` : ""}`}>
                  <div className="truncate text-fg-secondary">
                    {p.name}
                    {p.user && <span className="ml-1.5 text-fg-muted">{p.user}</span>}
                  </div>
                  {p.cmd && <div className="truncate text-[10px] text-fg-muted">{p.cmd}</div>}
                </td>
                <td className="text-right tabular-nums">{p.cpu_pct.toFixed(1)}%</td>
                <td className="whitespace-nowrap pl-2 text-right tabular-nums">{formatBytes(p.rss_bytes)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <Card title={`Listening ports (${f.listening.length})`}>
        <div className="max-h-56 overflow-auto">
          <table className="w-full font-mono text-xs">
            <tbody>
              {f.listening.map((l) => (
                <tr key={`${l.proto}${l.addr}${l.port}`}>
                  <td className="py-0.5 pr-2 text-fg-muted">{l.proto}</td>
                  <td className="pr-2 text-right tabular-nums">{l.port}</td>
                  <td className="pr-2 text-fg-secondary">{l.addr}</td>
                  <td className="truncate text-fg-secondary">{l.process}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="Systemd">
        {f.failed_units.length === 0 ? (
          <p className="text-xs text-fg-secondary">No failed units.</p>
        ) : (
          <ul className="space-y-0.5 font-mono text-xs text-warn">
            {f.failed_units.map((u) => (
              <li key={u}>{u}</li>
            ))}
          </ul>
        )}
        <p className="mt-2 text-[11px] text-fg-muted">
          Up {formatDuration(f.uptime_s)} · facts {ago(agent.facts_at)}
        </p>
      </Card>
    </div>
  );
}

function NodeView({ id }: { id: string }) {
  const { can } = useAuth();
  const agent = useResource<AgentDetail>(`/agents/${id}`);
  // bumped when a template is applied: the checks list is read again
  const [checksRev, setChecksRev] = useState(0);
  const a = agent.data;
  if (agent.error) return <Alert>{agent.error}</Alert>;
  if (!a) return <p className="text-sm text-fg-muted">Loading…</p>;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">{a.node_name}</h1>
        <span className="font-mono text-xs text-fg-muted">{a.address}</span>
        <EnvBadge env={a.environment} />
        <div className="ml-auto flex gap-2">
          {a.has_ssh && can("ssh.connect") && (
            <Link href={`/terminal/?node=${a.node_id}`} className="flex items-center gap-1 rounded border border-line bg-elevated px-2 py-1 text-xs text-telemetry hover:border-line-strong">
              <Terminal className="h-3 w-3" aria-hidden /> Terminal
            </Link>
          )}
          {a.has_ssh && can("sftp.transfer") && (
            <Link href={`/terminal/?view=files&node=${a.node_id}`} className="flex items-center gap-1 rounded border border-line bg-elevated px-2 py-1 text-xs text-primary hover:border-line-strong">
              <FolderOpen className="h-3 w-3" aria-hidden /> Files
            </Link>
          )}
        </div>
      </div>
      <AgentCard agent={a} reload={agent.reload} />
      {a.enrolled_at && (
        <TemplateSuggestions nodeId={a.node_id} hasFacts={!!a.facts} onApplied={() => setChecksRev((n) => n + 1)} />
      )}
      <ChecksCard key={checksRev} nodeId={a.node_id} nodeName={a.node_name} />
      {a.enrolled_at && <Charts nodeId={a.node_id} />}
      <Facts agent={a} />
    </div>
  );
}

function NodePage() {
  const id = useSearchParams().get("id");
  if (!id) return <Alert>No node selected.</Alert>;
  return <NodeView key={id} id={id} />;
}

export default function Page() {
  return (
    <Suspense fallback={<p className="text-sm text-fg-muted">Loading…</p>}>
      <NodePage />
    </Suspense>
  );
}
