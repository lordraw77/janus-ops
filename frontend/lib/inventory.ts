/** Inventory and vault types shared by the console pages. */

export type Credential = {
  id: string;
  name: string;
  kind: string;
  username: string | null;
  description: string | null;
  public_info: { key_type?: string; fingerprint?: string };
  used_by: { node_id: string; node_name: string; capability: string }[];
  created_at: string;
  updated_at: string;
};

export const KIND_LABELS: Record<string, string> = {
  ssh_key: "SSH private key",
  ssh_password: "SSH password",
  sudo_password: "sudo password",
  proxmox_token: "Proxmox API token",
  docker_registry: "Docker registry",
  snmp_v3: "SNMPv3",
  notification_secret: "Notification secret",
  generic: "Generic secret",
};

export type CapabilityKind = "ssh" | "agent" | "proxmox" | "docker" | "snmp";

export type Capability = {
  kind: CapabilityKind;
  enabled: boolean;
  config: Record<string, unknown>;
  credential: { id: string; name: string; kind: string } | null;
};

export type InventoryNode = {
  id: string;
  name: string;
  address: string;
  environment: "prod" | "staging" | "lab" | "dmz";
  parent: { id: string; name: string } | null;
  description: string | null;
  tags: string[];
  groups: string[];
  capabilities: Capability[];
  created_at: string;
  updated_at: string;
};

export type Group = {
  id: string;
  name: string;
  description: string | null;
  ansible_vars: Record<string, unknown>;
  node_count: number;
};

export type Tag = { id: string; name: string; node_count: number };

export const ENVIRONMENTS = ["prod", "staging", "lab", "dmz"] as const;

/** Capability -> label, accepted credential kinds (empty = none), credential required. */
export const CAPABILITIES: Record<CapabilityKind, { label: string; credentials: string[]; required: boolean }> = {
  ssh: { label: "SSH target", credentials: ["ssh_key", "ssh_password"], required: true },
  agent: { label: "Janus agent", credentials: [], required: false },
  proxmox: { label: "Proxmox node", credentials: ["proxmox_token"], required: true },
  docker: { label: "Docker host", credentials: [], required: false },
  snmp: { label: "SNMP device", credentials: ["snmp_v3", "generic"], required: true },
};

export const ENV_STYLE: Record<string, string> = {
  prod: "border-crit/40 text-crit",
  staging: "border-warn/40 text-warn",
  lab: "border-telemetry/40 text-telemetry",
  dmz: "border-ai/40 text-ai",
};
