"use client";

import { Trash2 } from "lucide-react";

import { Button, Field, TextInput } from "@/components/ui";
import type { Credential, NodeService } from "@/lib/inventory";

/** One field of a service type's config, as the backend catalog describes it. */
export type ServiceField = {
  name: string;
  label: string;
  type: "number" | "text" | "select";
  options?: { value: string; label: string }[];
};

/** A kind of service a node can have (GET /service-types): the form is drawn from this. */
export type ServiceType = {
  kind: string;
  label: string;
  description: string;
  credentials: string[];
  credential_required: boolean;
  fields: ServiceField[];
  defaults: Record<string, string | number>;
};

/** A service in the form: every config value is text until it is sent. */
export type ServiceState = {
  key: string;
  kind: string;
  name: string;
  enabled: boolean;
  credential: string;
  config: Record<string, string>;
};

let counter = 0;
const newKey = () => `svc-${(counter += 1)}`;

const SELECT =
  "w-full rounded border border-line bg-canvas px-3 py-1.5 text-sm outline-none focus:border-primary";

export function servicesFrom(existing: NodeService[]): ServiceState[] {
  return existing.map((s) => ({
    key: newKey(),
    kind: s.kind,
    // a service called like its kind is the common case: the name field stays empty
    name: s.name === s.kind ? "" : s.name,
    enabled: s.enabled,
    credential: s.credential?.id ?? "",
    config: Object.fromEntries(Object.entries(s.config).map(([k, v]) => [k, String(v ?? "")])),
  }));
}

export function newService(type: ServiceType): ServiceState {
  return {
    key: newKey(),
    kind: type.kind,
    name: "",
    enabled: true,
    credential: "",
    config: Object.fromEntries(Object.entries(type.defaults).map(([k, v]) => [k, String(v)])),
  };
}

/** The `services` of a node request. */
export function servicesPayload(list: ServiceState[], types: ServiceType[]) {
  return list.map((s) => {
    const fields = types.find((t) => t.kind === s.kind)?.fields ?? [];
    const config: Record<string, unknown> = {};
    for (const f of fields) {
      const v = s.config[f.name];
      if (v === undefined || v === "") continue;
      config[f.name] = f.type === "number" ? Number(v) : v;
    }
    return {
      kind: s.kind,
      ...(s.name.trim() ? { name: s.name.trim() } : {}),
      enabled: s.enabled,
      config,
      credential_id: s.credential || null,
    };
  });
}

/** What a node runs that Janus-Ops reads over the network: any number, each with its own
 * name, settings and credential. The list of kinds comes from the server, so a new kind needs
 * no change here. */
export function ServicesEditor({
  types,
  value,
  onChange,
  credentials,
}: {
  types: ServiceType[];
  value: ServiceState[];
  onChange: (next: ServiceState[]) => void;
  credentials: Credential[];
}) {
  const patch = (key: string, change: Partial<ServiceState>) =>
    onChange(value.map((s) => (s.key === key ? { ...s, ...change } : s)));
  const setConfig = (s: ServiceState, name: string, v: string) => patch(s.key, { config: { ...s.config, [name]: v } });

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-fg-secondary">Services</span>
        {types.length > 0 && (
          <select
            aria-label="Add a service"
            value=""
            onChange={(e) => {
              const type = types.find((t) => t.kind === e.target.value);
              if (type) onChange([...value, newService(type)]);
            }}
            className="rounded border border-line bg-elevated px-2 py-1 text-xs"
          >
            <option value="">+ Add a service…</option>
            {types.map((t) => (
              <option key={t.kind} value={t.kind}>
                {t.label}
              </option>
            ))}
          </select>
        )}
      </div>
      {value.length === 0 && (
        <p className="text-[11px] text-fg-muted">
          None. A service is something the node runs that Janus-Ops reads over the network, like a database.
        </p>
      )}
      {value.map((s) => {
        const type = types.find((t) => t.kind === s.kind);
        if (!type) return null;
        const usable = credentials.filter((c) => type.credentials.includes(c.kind));
        return (
          <div key={s.key} className={`rounded border p-2 ${s.enabled ? "border-line-strong bg-elevated/50" : "border-line"}`}>
            <div className="flex items-center gap-2 text-xs font-semibold">
              {type.label}
              <label className="ml-auto flex items-center gap-1 font-normal text-fg-secondary">
                <input type="checkbox" checked={s.enabled} onChange={(e) => patch(s.key, { enabled: e.target.checked })} className="accent-primary" />
                enabled
              </label>
              <Button type="button" variant="danger" aria-label={`Remove ${type.label}`} onClick={() => onChange(value.filter((x) => x.key !== s.key))}>
                <Trash2 className="h-3.5 w-3.5" aria-hidden />
              </Button>
            </div>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <Field label="Name" hint={`Empty = ${type.kind}. Needed to tell two of the same kind apart.`}>
                <TextInput value={s.name} onChange={(e) => patch(s.key, { name: e.target.value })} placeholder={type.kind} />
              </Field>
              <Field label={`Credential (${type.credentials.join(" or ")})`}>
                <select value={s.credential} onChange={(e) => patch(s.key, { credential: e.target.value })} className={SELECT}>
                  <option value="">{type.credential_required ? "— select —" : "—"}</option>
                  {usable.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                      {c.username ? ` (${c.username})` : ""}
                    </option>
                  ))}
                </select>
              </Field>
              {type.fields.map((f) => (
                <Field key={f.name} label={f.label}>
                  {f.type === "select" ? (
                    <select value={s.config[f.name] ?? ""} onChange={(e) => setConfig(s, f.name, e.target.value)} className={SELECT}>
                      {(f.options ?? []).map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <TextInput
                      inputMode={f.type === "number" ? "numeric" : undefined}
                      value={s.config[f.name] ?? ""}
                      onChange={(e) => setConfig(s, f.name, e.target.value)}
                    />
                  )}
                </Field>
              ))}
              <p className="col-span-2 text-[11px] text-fg-muted">{type.description}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}
