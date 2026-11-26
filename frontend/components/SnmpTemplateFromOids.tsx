"use client";

import Link from "next/link";
import { useState } from "react";

import type { DeviceType, OidEntry } from "@/components/SnmpOids";
import { Alert, Button, Field, Modal, Select, TextInput } from "@/components/ui";
import { api, message } from "@/lib/api";

/** The template category that fits a device type of the OID catalog. */
const CATEGORY: Record<DeviceType, string> = {
  nas: "nas",
  ups: "ups",
  switch: "network",
  router: "network",
  server: "server",
  generic: "generic",
};

/** One check per OID; two entries with the same name get their OID appended so every name is unique. */
export function itemsFromOids(entries: OidEntry[]) {
  const seen = new Set<string>();
  return entries.map((e) => {
    const plain = e.name.trim().slice(0, 240);
    const name = seen.has(plain.toLowerCase()) ? `${plain} [${e.oid}]` : plain;
    seen.add(plain.toLowerCase());
    return {
      name,
      kind: "snmp",
      params: { oid: e.oid.replace(/^\./, "") },
      indexed: e.indexed,
      interval_s: 60,
      check_cycles: 1,
    };
  });
}

/**
 * Builds a check template from catalog entries. Thresholds are left out on purpose: they depend on
 * the unit and the device, and the Templates page edits them (with macros) once it exists.
 */
export function SnmpTemplateFromOids({
  entries,
  onClose,
}: {
  entries: OidEntry[];
  onClose: () => void;
}) {
  const first = entries[0]?.device_type ?? "generic";
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState(CATEGORY[first]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState(false);

  async function create() {
    setBusy(true);
    setError(null);
    const r = await api("/templates", {
      method: "POST",
      body: {
        name: name.trim(),
        category,
        description: description.trim(),
        requires: "snmp",
        macros: [],
        items: itemsFromOids(entries),
      },
    });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    setCreated(true);
  }

  return (
    <Modal title="Template from the selected OIDs" onClose={onClose}>
      {created ? (
        <div className="space-y-3">
          <Alert kind="ok">Template created with {entries.length} checks.</Alert>
          <p className="text-xs text-fg-muted">
            Open it on the{" "}
            <Link href="/templates/" className="text-primary">
              Templates page
            </Link>{" "}
            to set warning and critical thresholds, then apply it to your SNMP nodes.
          </p>
          <div className="flex justify-end">
            <Button type="button" onClick={onClose}>
              Close
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          <Field label="Template name">
            <TextInput autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Synology NAS health" />
          </Field>
          <Field label="Category">
            <Select value={category} onChange={(e) => setCategory(e.target.value)}>
              {["nas", "ups", "network", "server", "infra", "generic"].map((c) => (
                <option key={c}>{c}</option>
              ))}
            </Select>
          </Field>
          <Field label="Description" hint="Optional">
            <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
          <ul className="max-h-40 space-y-0.5 overflow-y-auto rounded border border-line p-2 text-xs">
            {entries.map((e) => (
              <li key={e.id} className="flex justify-between gap-3">
                <span className="truncate">{e.name}</span>
                <span className="shrink-0 font-mono text-[11px] text-fg-muted">{e.oid}</span>
              </li>
            ))}
          </ul>
          {error && <Alert>{error}</Alert>}
          <div className="flex justify-end gap-2">
            <Button type="button" onClick={onClose}>
              Cancel
            </Button>
            <Button type="button" variant="primary" disabled={busy || !name.trim()} onClick={() => void create()}>
              {busy ? "Creating…" : `Create with ${entries.length} checks`}
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
