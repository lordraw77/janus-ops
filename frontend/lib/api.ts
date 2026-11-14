import { useCallback, useEffect, useState } from "react";

/** Minimal JSON client for the Janus-Ops API (same origin, session cookie). */

export type ApiResult<T> =
  | { ok: true; status: number; data: T }
  | { ok: false; status: number; code: string; retryAfter?: number };

export async function api<T = unknown>(
  path: string,
  options: { method?: string; body?: unknown } = {},
): Promise<ApiResult<T>> {
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, {
      method: options.method ?? "GET",
      headers: options.body === undefined ? undefined : { "Content-Type": "application/json" },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      cache: "no-store",
    });
  } catch {
    return { ok: false, status: 0, code: "network_error" };
  }
  const text = await res.text();
  const json: unknown = text ? JSON.parse(text) : null;
  if (res.ok) return { ok: true, status: res.status, data: json as T };

  let code = `http_${res.status}`;
  const detail = (json as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") code = detail;
  else if (res.status === 422) code = "invalid_input";
  const retry = Number(res.headers.get("Retry-After"));
  return { ok: false, status: res.status, code, retryAfter: retry > 0 ? retry : undefined };
}

const MESSAGES: Record<string, string> = {
  network_error: "The Janus-Ops API is not reachable.",
  invalid_input: "Some fields are not valid.",
  invalid_credentials: "Invalid username or password.",
  no_role: "Your account is not a member of any Janus-Ops role group. Ask an administrator.",
  directory_unavailable:
    "The user directory is unavailable. Try again later or use the break-glass account.",
  account_disabled: "This account is disabled.",
  account_conflict: "This username is already linked to another account.",
  invalid_code: "The code is not valid. Check the time on your phone and try again.",
  mfa_locked: "Too many wrong codes. Sign in again.",
  not_authenticated: "Your session has expired. Sign in again.",
  forbidden: "You do not have permission for this action.",
  admin_only: "Only administrators can grant the admin role or change administrators.",
  already_exists: "A user with this name or e-mail already exists.",
  reserved_name: "This name is reserved for a service account.",
  cannot_delete_self: "You cannot delete your own account.",
  cannot_disable_self: "You cannot disable your own account.",
  cannot_remove_own_admin: "You cannot remove the admin role from yourself.",
  directory_read_only: "Users are managed in an external directory and cannot be changed here.",
  admin_role_fixed: "The admin role always has every permission.",
  builtin_role: "Built-in roles cannot be deleted.",
  role_or_group_exists: "A role with this name or LDAP group already exists.",
  group_in_use: "This LDAP group is already mapped to another role.",
  totp_unavailable: "Two-factor authentication is not configured on this server (JANUS_SECRET_KEY).",
  totp_already_enabled: "Two-factor authentication is already enabled.",
  totp_not_started: "Start the setup again.",
  totp_secret_unreadable: "The stored TOTP secret cannot be read. Ask an administrator to reset it.",
  not_found: "Not found.",
  name_exists: "This name is already used.",
  name_required: "A name is required.",
  name_and_address_required: "Name and address are required.",
  address_required: "An address is required.",
  environment_required: "An environment is required.",
  parent_cycle: "This parent would create a loop (a node cannot depend on itself).",
  unknown_parent: "The parent node does not exist.",
  unknown_credential: "The selected credential does not exist.",
  docker_via_ssh_needs_ssh: "Docker over SSH needs the SSH capability on the same node.",
  jump_host_without_ssh: "The jump host must have the SSH capability enabled.",
  credential_in_use: "This credential is used by some nodes: detach it first.",
  vault_unavailable: "The vault is not configured on this server (JANUS_SECRET_KEY).",
  name_and_body_required: "A name and a command are required.",
  invalid_path: "The path is not valid (it must be absolute).",
  no_such_path: "No such file or directory on the server.",
  remote_permission_denied: "The remote account is not allowed to do this.",
  exists: "A file or directory with this name already exists.",
  directory_not_empty: "The directory is not empty.",
  not_a_file: "Only regular files can be downloaded.",
  too_large: "The file is larger than the upload limit of this server.",
  sftp_error: "The SFTP server refused the operation.",
  sftp_unavailable: "The server does not offer SFTP.",
  connection_lost: "The SSH connection was lost. Try again.",
  auth_failed: "The server refused the credential.",
  unreachable: "The server is not reachable.",
  host_key_mismatch: "The server's host key changed: reset the pin in Host keys if expected.",
  no_ssh_capability: "This node has no SSH capability.",
  // agents
  no_agent_capability: "Enable the Janus agent capability on this node first.",
  agent_binary_missing: "The agent binary for this architecture is not available on the server.",
  agent_server_url_not_https: "Agents need an https:// server URL (JANUS_AGENT_SERVER_URL).",
  bootstrap_running: "An installation is already running on this node.",
  needs_root: "The SSH account must be root or have passwordless sudo.",
  unsupported_arch: "Unsupported CPU architecture: the agent runs on amd64 and arm64.",
  systemd_required: "The agent needs a systemd host.",
  upload_failed: "Copying the agent to the node failed.",
  install_failed: "Installing the agent service failed: see the audit log.",
  uninstall_failed: "Removing the agent failed.",
  remote_command_failed: "A command on the node failed.",
  not_enrolled: "This agent is not enrolled.",
  ssh_no_ssh_capability: "This node has no SSH capability: it is needed to install the agent.",
  ssh_unreachable: "The node is not reachable over SSH.",
  ssh_auth_failed: "SSH authentication failed: check the node's credential.",
  ssh_host_key_mismatch: "The node's SSH host key changed: reset the pin in Host keys if expected.",
  ssh_no_credential: "The SSH capability has no credential.",
};

export function message(code: string, retryAfter?: number): string {
  if (code === "too_many_attempts") {
    const minutes = Math.ceil((retryAfter ?? 60) / 60);
    return `Too many failed attempts. Try again in ${minutes} minute${minutes === 1 ? "" : "s"}.`;
  }
  if (code.startsWith("unknown_role:")) return `Unknown role: ${code.slice(13)}.`;
  const colon = code.indexOf(":");
  const prefix = colon > 0 ? code.slice(0, colon) : code;
  const rest = colon > 0 ? code.slice(colon + 1) : undefined;
  const DETAILED: Record<string, string> = {
    invalid_secret: "Invalid secret",
    invalid_username: "User name format",
    unknown_group: "Unknown group",
    credential_required: "A credential is required for capability",
    credential_kind_mismatch: "The credential type does not fit capability",
    credential_not_used: "No credential is used by capability",
    invalid_config: "Invalid setting",
  };
  if (rest !== undefined && DETAILED[prefix]) return `${DETAILED[prefix]}: ${rest}.`;
  if (code.startsWith("unknown_permission:")) return `Unknown permission: ${code.slice(19)}.`;
  return MESSAGES[code] ?? `Request failed (${code}).`;
}

export type Resource<T> = {
  data: T | null;
  error: string | null;
  /** Fetch again (e.g. after a change). */
  reload: () => void;
};

/** GET `path` on mount and on every reload(); ignores responses of stale requests. */
export function useResource<T>(path: string): Resource<T> {
  const [version, setVersion] = useState(0);
  const [state, setState] = useState<{ data: T | null; error: string | null }>({
    data: null,
    error: null,
  });

  useEffect(() => {
    let cancelled = false;
    void api<T>(path).then((r) => {
      if (cancelled) return;
      setState(r.ok ? { data: r.data, error: null } : { data: null, error: message(r.code) });
    });
    return () => {
      cancelled = true;
    };
  }, [path, version]);

  const reload = useCallback(() => setVersion((v) => v + 1), []);
  return { ...state, reload };
}
