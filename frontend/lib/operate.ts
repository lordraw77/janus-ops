export type OperableNode = {
  id: string;
  name: string;
  environment: string;
  linux: string | null;
  proxmox: string | null;
};

export type OperateStep = {
  seq: number;
  target: string | null;
  server: "linux" | "proxmox";
  tool: string;
  mode: "read" | "write";
  arguments: Record<string, unknown>;
  result: string | null;
  error: string | null;
  duration_ms: number | null;
};

export type Provider = {
  slot: "main" | "review";
  label: string;
  host: string | null;
  model: string | null;
  available: boolean;
};

export type OperateMessage = {
  id: number;
  model: string | null;
  role: "user" | "assistant";
  content: string;
  status: "pending" | "done" | "error";
  error: string | null;
  read_only: boolean;
  created_at: string;
  steps: OperateStep[];
};

export type OperateSessionRow = {
  id: string;
  title: string;
  node_names: string[];
  archived: boolean;
  provider: "main" | "review";
  model: string | null;
  spec: string | null;
  created_at: string;
  updated_at: string;
};

export type OperateSession = OperateSessionRow & { messages: OperateMessage[] };

export const EXAMPLES = [
  "Show the disk usage and the biggest directories under /var",
  "Change the NTP server to ntp.example.org and check that time is in sync",
  "Check which services failed and why",
  "Update the package index and list what can be upgraded",
];

export function ready(n: OperableNode): boolean {
  return n.linux === "ok" || n.proxmox === "ok";
}
