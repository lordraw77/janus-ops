/** Proxmox VE API types (backend /api/v1/proxmox) and small formatting helpers. */

export type Cluster = {
  key: string;
  name: string;
  standalone: boolean;
  quorate: boolean | null;
  error: string | null;
  endpoints: { node_id: string; node_name: string; address: string }[];
};

export type PveNode = {
  node: string;
  status: string;
  ip: string | null;
  cpu: number | null;
  maxcpu: number | null;
  mem: number | null;
  maxmem: number | null;
  disk: number | null;
  maxdisk: number | null;
  uptime: number | null;
  inventory_id: string | null;
};

export type Storage = {
  storage: string;
  node: string;
  status: string;
  type: string;
  shared: boolean;
  content: string[];
  disk: number | null;
  maxdisk: number | null;
};

export type GuestType = "qemu" | "lxc";

export type Guest = {
  vmid: number;
  name: string | null;
  type: GuestType;
  node: string;
  status: string;
  template: boolean;
  cpu: number | null;
  maxcpu: number | null;
  mem: number | null;
  maxmem: number | null;
  disk: number | null;
  maxdisk: number | null;
  netin: number | null;
  netout: number | null;
  uptime: number | null;
  tags: string[];
  pool: string | null;
  hastate: string | null;
  lock: string | null;
};

export type Overview = {
  cluster: {
    name: string;
    standalone: boolean;
    quorate: boolean;
    nodes_online: number;
    nodes_total: number;
    version: string | null;
  };
  nodes: PveNode[];
  storage: Storage[];
  guests: Guest[];
};

export type Snapshot = {
  name: string;
  description: string;
  time: number | null;
  parent: string | null;
  vmstate: boolean;
  current: boolean;
};

export type GuestDetail = {
  status: Record<string, unknown>;
  config: Record<string, string | number>;
  serial: boolean;
  snapshots: Snapshot[];
};

export type BackupStatus = "failed" | "missing" | "stale" | "unscheduled" | "ok";

export type BackupRow = {
  vmid: number;
  name: string | null;
  type: GuestType;
  node: string;
  status: BackupStatus;
  jobs: string[];
  last_backup: number | null;
  age_s: number | null;
  max_age_s: number;
  size: number | null;
  storage: string | null;
  verified: string | null;
  archives: number;
  failed_at: number | null;
};

export type BackupAudit = {
  max_age_h: number | null;
  summary: Partial<Record<BackupStatus, number>>;
  guests: BackupRow[];
  jobs: {
    id: string;
    enabled: boolean;
    schedule: string | null;
    storage: string | null;
    node: string | null;
    selection: string | null;
    exclude: string | null;
    mode: string | null;
    comment: string | null;
  }[];
  failed_tasks: { upid: string; node: string; id: string; status: string; starttime: number; endtime: number | null }[];
  storages: string[];
  errors: string[];
};

export type Task = {
  upid: string;
  node: string;
  type: string;
  id: string;
  user: string;
  status: string | null;
  starttime: number;
  endtime: number | null;
};

export type TaskDetail = {
  status: "running" | "stopped";
  exitstatus: string | null;
  type: string;
  id: string;
  starttime: number;
  log: string[];
};

/** "UPID:..." plus the PVE node that runs it: what every lifecycle call returns. */
export type Started = { upid: string; node: string };

export const BACKUP_STYLE: Record<BackupStatus, { label: string; className: string }> = {
  failed: { label: "Failed", className: "border-crit/40 bg-crit/15 text-crit" },
  missing: { label: "No backup", className: "border-crit/40 bg-crit/15 text-crit" },
  stale: { label: "Too old", className: "border-warn/40 bg-warn/15 text-warn" },
  unscheduled: { label: "No job", className: "border-warn/40 bg-warn/15 text-warn" },
  ok: { label: "OK", className: "border-ok/40 bg-ok/15 text-ok" },
};

export const TASK_LABEL: Record<string, string> = {
  qmstart: "VM start",
  qmstop: "VM stop",
  qmshutdown: "VM shutdown",
  qmreboot: "VM reboot",
  qmsuspend: "VM suspend",
  qmresume: "VM resume",
  qmsnapshot: "VM snapshot",
  qmdelsnapshot: "VM delete snapshot",
  qmrollback: "VM rollback",
  qmclone: "VM clone",
  qmigrate: "VM migrate",
  vzstart: "CT start",
  vzstop: "CT stop",
  vzshutdown: "CT shutdown",
  vzreboot: "CT reboot",
  vzsnapshot: "CT snapshot",
  vzdelsnapshot: "CT delete snapshot",
  vzrollback: "CT rollback",
  vzclone: "CT clone",
  vzmigrate: "CT migrate",
  vzdump: "Backup",
  vncshell: "Shell",
  vncproxy: "Console",
  termproxy: "Console",
  aptupdate: "Package update",
  srvreload: "Service reload",
  srvrestart: "Service restart",
};

export function pct(used: number | null | undefined, max: number | null | undefined): number | null {
  if (used === null || used === undefined || !max) return null;
  return Math.min(100, (used / max) * 100);
}

export function when(epoch: number | null | undefined): string {
  if (!epoch) return "–";
  return new Date(epoch * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
}
