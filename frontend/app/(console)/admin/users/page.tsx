"use client";

import { Lock, Pencil, Plus, Trash2 } from "lucide-react";
import { type FormEvent, type ReactNode, useState } from "react";

import { Alert, Button, Card, Field, Modal, RoleBadge, TextInput } from "@/components/ui";
import { api, message, useResource } from "@/lib/api";
import { RequirePermission, useAuth } from "@/lib/auth";
import { type NodeScope, type ScopeRule, ScopeEditor } from "@/components/ScopeEditor";

type User = {
  id: string;
  display_name: string;
  email: string;
  roles: string[];
  source: "directory" | "breakglass" | "external";
  created_at: string | null;
  last_login_at: string | null;
  disabled: boolean;
  totp_enabled: boolean;
};
type Role = {
  name: string;
  description: string;
  ldap_group: string;
  builtin: boolean;
  permissions: string[];
  node_scope: NodeScope;
  scope_rules: ScopeRule[];
};
type RoleList = { roles: Role[]; permissions: Record<string, string>; managed: boolean };

function when(ts: string | null): string {
  return ts ? new Date(ts).toLocaleString() : "never";
}

function RolePicker({
  roles,
  value,
  onChange,
  allowAdmin,
}: {
  roles: Role[];
  value: string[];
  onChange: (v: string[]) => void;
  allowAdmin: boolean;
}) {
  return (
    <div className="flex flex-wrap gap-3">
      {roles.map((r) => {
        const locked = r.name === "admin" && !allowAdmin;
        return (
          <label key={r.name} className={`flex items-center gap-1.5 text-xs ${locked ? "opacity-50" : ""}`}>
            <input
              type="checkbox"
              disabled={locked}
              checked={value.includes(r.name)}
              onChange={(e) =>
                onChange(e.target.checked ? [...value, r.name] : value.filter((x) => x !== r.name))
              }
              className="accent-primary"
            />
            {r.name}
          </label>
        );
      })}
    </div>
  );
}

type Dialog =
  | { kind: "create" }
  | { kind: "edit"; user: User }
  | { kind: "password"; user: User }
  | { kind: "confirm"; title: string; body: ReactNode; run: () => Promise<boolean> }
  | null;

function UsersTab({ roleList }: { roleList: RoleList }) {
  const { user: me, can } = useAuth();
  const { data, error, reload } = useResource<{ managed: boolean; users: User[] }>("/users");
  const users = data?.users ?? null;
  const managed = data?.managed ?? false;
  const [dialog, setDialog] = useState<Dialog>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const isAdmin = me.roles.includes("admin");
  const canManage = can("users.manage");

  async function call(path: string, method: string, body?: unknown): Promise<boolean> {
    setBusy(true);
    setFormError(null);
    const r = await api(path, { method, body });
    setBusy(false);
    if (!r.ok) {
      setFormError(message(r.code));
      return false;
    }
    setDialog(null);
    reload();
    return true;
  }

  function open(d: Dialog) {
    setFormError(null);
    setDialog(d);
  }

  const editable = (u: User) =>
    canManage && u.source === "directory" && managed && (isAdmin || !u.roles.includes("admin"));

  return (
    <Card
      title={`Users${users ? ` (${users.length})` : ""}`}
      actions={
        canManage && managed ? (
          <Button variant="primary" onClick={() => open({ kind: "create" })}>
            <Plus className="h-3.5 w-3.5" aria-hidden /> New user
          </Button>
        ) : null
      }
    >
      {!managed && users && (
        <div className="mb-3">
          <Alert kind="info">{message("directory_read_only")}</Alert>
        </div>
      )}
      {error && <Alert>{error}</Alert>}
      {users && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-line text-fg-muted">
              <tr>
                <th className="py-2 pr-3 font-medium">User</th>
                <th className="py-2 pr-3 font-medium">Roles</th>
                <th className="py-2 pr-3 font-medium">2FA</th>
                <th className="py-2 pr-3 font-medium">Last sign-in</th>
                <th className="py-2 pr-3 font-medium">Status</th>
                <th className="py-2 font-medium" />
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {users.map((u) => (
                <tr key={u.id} className={u.disabled ? "opacity-60" : ""}>
                  <td className="py-2 pr-3">
                    <div className="font-semibold">{u.display_name || u.id}</div>
                    <div className="font-mono text-[11px] text-fg-muted">
                      {u.id}
                      {u.email ? ` · ${u.email}` : ""}
                    </div>
                  </td>
                  <td className="py-2 pr-3">
                    <div className="flex flex-wrap gap-1">
                      {u.roles.length ? u.roles.map((r) => <RoleBadge key={r} role={r} />) : (
                        <span className="text-fg-muted">no access</span>
                      )}
                    </div>
                  </td>
                  <td className="py-2 pr-3">{u.totp_enabled ? <span className="text-ok">on</span> : "off"}</td>
                  <td className="py-2 pr-3 text-fg-secondary">{when(u.last_login_at)}</td>
                  <td className="py-2 pr-3">
                    {u.source === "breakglass" ? (
                      <span className="text-warn">break-glass</span>
                    ) : u.disabled ? (
                      <span className="text-crit">disabled</span>
                    ) : (
                      <span className="text-ok">active</span>
                    )}
                  </td>
                  <td className="py-2 text-right">
                    {editable(u) ? (
                      <div className="flex justify-end gap-1">
                        <Button onClick={() => open({ kind: "edit", user: u })} aria-label={`Edit ${u.id}`}>
                          <Pencil className="h-3 w-3" aria-hidden /> Edit
                        </Button>
                        <Button onClick={() => open({ kind: "password", user: u })}>Password</Button>
                        {u.totp_enabled && (
                          <Button
                            onClick={() =>
                              open({
                                kind: "confirm",
                                title: `Reset 2FA for ${u.id}?`,
                                body: "The user signs in with the password only and can set up 2FA again.",
                                run: () => call(`/users/${u.id}/totp`, "DELETE"),
                              })
                            }
                          >
                            Reset 2FA
                          </Button>
                        )}
                        {u.id !== me.username && (
                          <Button
                            variant="danger"
                            aria-label={`Delete ${u.id}`}
                            onClick={() =>
                              open({
                                kind: "confirm",
                                title: `Delete ${u.id}?`,
                                body: "The account is removed from the directory. This cannot be undone.",
                                run: () => call(`/users/${u.id}`, "DELETE"),
                              })
                            }
                          >
                            <Trash2 className="h-3 w-3" aria-hidden />
                          </Button>
                        )}
                      </div>
                    ) : canManage && u.source === "breakglass" && isAdmin && u.totp_enabled ? (
                      <Button
                        onClick={() =>
                          open({
                            kind: "confirm",
                            title: "Reset 2FA of the break-glass account?",
                            body: "Its password is unchanged; set it with `make breakglass`.",
                            run: () => call(`/users/${u.id}/totp`, "DELETE"),
                          })
                        }
                      >
                        Reset 2FA
                      </Button>
                    ) : (
                      <Lock className="ml-auto h-3 w-3 text-fg-muted" aria-label="Read-only" />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {dialog?.kind === "create" && (
        <CreateUser
          roles={roleList.roles}
          allowAdmin={isAdmin}
          busy={busy}
          error={formError}
          onClose={() => setDialog(null)}
          onSubmit={(body) => call("/users", "POST", body)}
        />
      )}
      {dialog?.kind === "edit" && (
        <EditUser
          user={dialog.user}
          self={dialog.user.id === me.username}
          roles={roleList.roles}
          allowAdmin={isAdmin}
          busy={busy}
          error={formError}
          onClose={() => setDialog(null)}
          onSubmit={(body) => call(`/users/${dialog.user.id}`, "PATCH", body)}
        />
      )}
      {dialog?.kind === "password" && (
        <PasswordDialog
          user={dialog.user}
          busy={busy}
          error={formError}
          onClose={() => setDialog(null)}
          onSubmit={(password) => call(`/users/${dialog.user.id}/password`, "POST", { password })}
        />
      )}
      {dialog?.kind === "confirm" && (
        <Modal title={dialog.title} onClose={() => setDialog(null)}>
          <p className="mb-4 text-sm text-fg-secondary">{dialog.body}</p>
          {formError && <Alert>{formError}</Alert>}
          <div className="mt-4 flex justify-end gap-2">
            <Button onClick={() => setDialog(null)}>Cancel</Button>
            <Button variant="danger" disabled={busy} onClick={() => void dialog.run()}>
              Confirm
            </Button>
          </div>
        </Modal>
      )}
    </Card>
  );
}

type FormProps<T> = { busy: boolean; error: string | null; onClose: () => void; onSubmit: (v: T) => Promise<boolean> };

function CreateUser({
  roles,
  allowAdmin,
  busy,
  error,
  onClose,
  onSubmit,
}: FormProps<Record<string, unknown>> & { roles: Role[]; allowAdmin: boolean }) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  function submit(e: FormEvent) {
    e.preventDefault();
    void onSubmit({ id: id.trim(), display_name: name.trim(), email: email.trim(), password, roles: picked });
  }
  return (
    <Modal title="New user" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Username" hint="Lower-case letters, digits, . _ - (2–64 characters)">
          <TextInput autoFocus required value={id} onChange={(e) => setId(e.target.value.toLowerCase())} />
        </Field>
        <Field label="Full name">
          <TextInput required value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="E-mail">
          <TextInput type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label="Initial password" hint="At least 12 characters">
          <TextInput type="password" autoComplete="new-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="Roles">
          <RolePicker roles={roles} value={picked} onChange={setPicked} allowAdmin={allowAdmin} />
        </Field>
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy || !id || !name || !email || password.length < 12}>
            Create
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function EditUser({
  user,
  self,
  roles,
  allowAdmin,
  busy,
  error,
  onClose,
  onSubmit,
}: FormProps<Record<string, unknown>> & { user: User; self: boolean; roles: Role[]; allowAdmin: boolean }) {
  const [name, setName] = useState(user.display_name);
  const [email, setEmail] = useState(user.email);
  const [picked, setPicked] = useState<string[]>(user.roles);
  const [disabled, setDisabled] = useState(user.disabled);
  function submit(e: FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = {};
    if (name !== user.display_name) body.display_name = name.trim();
    if (email !== user.email) body.email = email.trim();
    if ([...picked].sort().join() !== [...user.roles].sort().join()) body.roles = picked;
    if (disabled !== user.disabled) body.disabled = disabled;
    if (Object.keys(body).length === 0) return onClose();
    void onSubmit(body);
  }
  return (
    <Modal title={`Edit ${user.id}`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Full name">
          <TextInput required value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="E-mail">
          <TextInput type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label="Roles" hint="Applied at the user's next sign-in">
          <RolePicker roles={roles} value={picked} onChange={setPicked} allowAdmin={allowAdmin} />
        </Field>
        {!self && (
          <label className="flex items-center gap-2 text-xs">
            <input type="checkbox" checked={disabled} onChange={(e) => setDisabled(e.target.checked)} className="accent-crit" />
            Disable access to Janus-Ops (ends open sessions)
          </label>
        )}
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function PasswordDialog({ user, busy, error, onClose, onSubmit }: FormProps<string> & { user: User }) {
  const [password, setPassword] = useState("");
  return (
    <Modal title={`New password for ${user.id}`} onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void onSubmit(password);
        }}
        className="space-y-3"
      >
        <Field label="Password" hint="At least 12 characters. Open sessions of the user are ended.">
          <TextInput type="password" autoComplete="new-password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy || password.length < 12}>
            Set password
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function RolesTab({ roleList, reload }: { roleList: RoleList; reload: () => void }) {
  const { can, user: me } = useAuth();
  const canManage = can("roles.manage");
  const isAdmin = me.roles.includes("admin");
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Role | "new" | null>(null);
  const perms = Object.entries(roleList.permissions);

  async function patch(name: string, body: unknown): Promise<boolean> {
    setError(null);
    const r = await api(`/roles/${name}`, { method: "PATCH", body });
    if (!r.ok) {
      setError(message(r.code));
      return false;
    }
    reload();
    return true;
  }

  async function remove(name: string) {
    setError(null);
    const r = await api(`/roles/${name}`, { method: "DELETE" });
    if (!r.ok) setError(message(r.code));
    reload();
  }

  return (
    <Card
      title="Roles & permissions"
      actions={
        canManage ? (
          <Button variant="primary" onClick={() => setEditing("new")}>
            <Plus className="h-3.5 w-3.5" aria-hidden /> New role
          </Button>
        ) : null
      }
    >
      <p className="mb-3 text-xs text-fg-secondary">
        A user gets a role by being a member of its LDAP group; with several roles the permissions
        add up. Permission changes apply immediately, also to signed-in users.
      </p>
      {error && <Alert>{error}</Alert>}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="border-b border-line text-fg-muted">
            <tr>
              <th className="py-2 pr-3 font-medium">Role</th>
              <th className="py-2 pr-3 font-medium">LDAP group</th>
              <th className="py-2 pr-3 font-medium">Nodes</th>
              {perms.map(([key, description]) => (
                <th key={key} className="py-2 pr-3 text-center font-mono font-medium" title={description}>
                  {key}
                </th>
              ))}
              <th />
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {roleList.roles.map((r) => {
              const fixed = r.name === "admin";
              return (
                <tr key={r.name}>
                  <td className="py-2 pr-3">
                    <RoleBadge role={r.name} />
                    <div className="mt-0.5 text-[11px] text-fg-muted">{r.description}</div>
                  </td>
                  <td className="py-2 pr-3 font-mono">{r.ldap_group}</td>
                  <td className="py-2 pr-3 text-fg-secondary" title={r.node_scope === "restricted" ? "Limited by rules: edit the role to see them" : undefined}>
                    {r.node_scope === "all" ? "all" : r.scope_rules.length === 0 ? "none" : `${r.scope_rules.length} rule${r.scope_rules.length === 1 ? "" : "s"}`}
                  </td>
                  {perms.map(([key]) => (
                    <td key={key} className="py-2 pr-3 text-center">
                      <input
                        type="checkbox"
                        aria-label={`${r.name}: ${key}`}
                        checked={r.permissions.includes(key)}
                        disabled={fixed || !canManage}
                        onChange={(e) =>
                          void patch(r.name, {
                            permissions: e.target.checked
                              ? [...r.permissions, key]
                              : r.permissions.filter((p) => p !== key),
                          })
                        }
                        className="accent-primary"
                      />
                    </td>
                  ))}
                  <td className="py-2 text-right">
                    {canManage && (!fixed || isAdmin) && (
                      <div className="flex justify-end gap-1">
                        <Button onClick={() => setEditing(r)} aria-label={`Edit role ${r.name}`}>
                          <Pencil className="h-3 w-3" aria-hidden />
                        </Button>
                        {!r.builtin && (
                          <Button variant="danger" onClick={() => void remove(r.name)} aria-label={`Delete role ${r.name}`}>
                            <Trash2 className="h-3 w-3" aria-hidden />
                          </Button>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <dl className="mt-4 grid grid-cols-[9rem_1fr] gap-y-1 text-[11px]">
        {perms.map(([key, description]) => (
          <div key={key} className="contents">
            <dt className="font-mono text-fg-secondary">{key}</dt>
            <dd className="text-fg-muted">{description}</dd>
          </div>
        ))}
      </dl>

      {editing && (
        <RoleDialog
          role={editing === "new" ? null : editing}
          permissions={roleList.permissions}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
    </Card>
  );
}

function RoleDialog({
  role,
  permissions,
  onClose,
  onSaved,
}: {
  role: Role | null;
  permissions: Record<string, string>;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(role?.name ?? "");
  const [description, setDescription] = useState(role?.description ?? "");
  const [group, setGroup] = useState(role?.ldap_group ?? "");
  const [picked, setPicked] = useState<string[]>(role?.permissions ?? []);
  const [scope, setScope] = useState<NodeScope>(role?.node_scope ?? "all");
  const [rules, setRules] = useState<ScopeRule[]>(role?.scope_rules ?? []);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const r = role
      ? await api(`/roles/${role.name}`, {
          method: "PATCH",
          body: {
            description,
            ...(group !== role.ldap_group ? { ldap_group: group.trim() } : {}),
            ...(role.name !== "admin" ? { permissions: picked, node_scope: scope, scope_rules: scope === "restricted" ? rules : [] } : {}),
          },
        })
      : await api("/roles", {
          method: "POST",
          body: {
            name: name.trim(),
            description,
            ...(group.trim() ? { ldap_group: group.trim() } : {}),
            permissions: picked,
            node_scope: scope,
            scope_rules: scope === "restricted" ? rules : [],
          },
        });
    setBusy(false);
    if (!r.ok) return setError(message(r.code));
    onSaved();
  }

  return (
    <Modal title={role ? `Edit role ${role.name}` : "New role"} onClose={onClose} wide>
      <form onSubmit={submit} className="space-y-3">
        {!role && (
          <Field label="Name" hint="Lower-case letters, digits and - (2–32 characters)">
            <TextInput autoFocus required value={name} onChange={(e) => setName(e.target.value.toLowerCase())} />
          </Field>
        )}
        <Field label="Description">
          <TextInput value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Field
          label="LDAP group"
          hint={role ? "Members of this group get the role" : `Default: janus-${name || "<name>"}; created in the directory if missing`}
        >
          <TextInput value={group} onChange={(e) => setGroup(e.target.value)} className="font-mono" />
        </Field>
        {role?.name !== "admin" && (
          <Field label="Permissions">
            <div className="space-y-1">
              {Object.entries(permissions).map(([key, desc]) => (
                <label key={key} className="flex items-start gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={picked.includes(key)}
                    onChange={(e) => setPicked(e.target.checked ? [...picked, key] : picked.filter((p) => p !== key))}
                    className="mt-0.5 accent-primary"
                  />
                  <span>
                    <span className="font-mono">{key}</span> — <span className="text-fg-muted">{desc}</span>
                  </span>
                </label>
              ))}
            </div>
          </Field>
        )}
        {role?.name !== "admin" && <ScopeEditor scope={scope} rules={rules} onScope={setScope} onRules={setRules} />}
        {error && <Alert>{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={busy || (!role && name.length < 2)}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export default function UsersAdminPage() {
  const [tab, setTab] = useState<"users" | "roles">("users");
  const { data: roleList, error, reload: loadRoles } = useResource<RoleList>("/roles");

  return (
    <RequirePermission permission="users.read">
      <div className="max-w-6xl space-y-4">
        <h1 className="text-lg font-semibold">Users & roles</h1>
        <div role="tablist" className="flex gap-1 border-b border-line text-xs">
          {(["users", "roles"] as const).map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={`-mb-px border-b-2 px-3 py-2 ${tab === t ? "border-primary text-primary" : "border-transparent text-fg-nav hover:text-fg"}`}
            >
              {t === "users" ? "Users" : "Roles & permissions"}
            </button>
          ))}
        </div>
        {error && <Alert>{error}</Alert>}
        {roleList &&
          (tab === "users" ? <UsersTab roleList={roleList} /> : <RolesTab roleList={roleList} reload={loadRoles} />)}
      </div>
    </RequirePermission>
  );
}
