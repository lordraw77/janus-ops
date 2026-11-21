"""Roles and permissions (stored in the `role` / `role_permission` tables).

Permissions are defined here, in code, because each one guards a code path. Roles are data:
admins create them, map them to an LDAP group and pick their permissions in the UI.
The built-in `admin` role always holds every permission and cannot be edited.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

ADMIN_ROLE = "admin"

PERMISSIONS: dict[str, str] = {
    "audit.read": "View the audit log and verify its integrity",
    "users.read": "View users, roles and permissions",
    "users.manage": "Create, edit, disable and delete users; reset passwords and TOTP",
    "roles.manage": "Create and delete roles; change their permissions and LDAP group",
    "inventory.read": "View nodes, tags, groups and credential names (never secrets)",
    "inventory.manage": "Create, edit and delete nodes, tags, groups and capabilities",
    "environments.manage": "Create, rename and delete environments (prod, staging, ...)",
    "credentials.manage": "Create, replace and delete vault credentials",
    "ssh.connect": "Open web SSH terminals on nodes with the SSH capability",
    "sftp.transfer": "Browse, download, upload, rename and delete files on nodes over SFTP",
    "snippets.manage": "Create, edit and delete shared command snippets",
    "agents.manage": "Install, revoke and uninstall the Janus agent on nodes (runs as root)",
    "monitoring.manage": "Create and change checks, schedule downtimes, acknowledge problems",
    "notifications.manage": "Configure notification channels (with their secrets) and rules",
    "containers.manage": "Start, stop, restart and remove containers and Compose stacks",
    "containers.exec": "Open interactive shells inside containers",
    "proxmox.manage": "Power, snapshot, clone and migrate Proxmox VMs and containers",
    "proxmox.console": "Open Proxmox consoles (VM serial, LXC; node shell also needs ssh.connect)",
    "ansible.read": "Browse playbook sources, files, versions and the run history",
    "ansible.manage": "Add, sync and delete playbook sources; edit and upload files",
    "ansible.run": "Run playbooks against nodes in scope (runs as the Janus-Ops host)",
    "ai.use": "Ask the AI Copilot to diagnose problems in scope (data goes to the AI endpoint)",
    "ai.manage": "Set the AI endpoints, models and API keys (keys are never shown again)",
    "ai.approve": "Approve, reject or edit the action plans proposed by the AI Copilot",
    "actions.execute": "Run approved action plans on nodes; the node's agent must allow exec",
    "ai.operate": "Let the AI Copilot run operational MCP tools (restart a service, power a VM)"
    " on nodes in scope; every call is audited. The admin role has it by default",
    "gitops.manage": "Create, change and delete GitOps stacks and rotate their webhook secrets",
    "gitops.deploy": "Deploy GitOps stacks now; the node's agent must allow exec",
    "workflow.manage": "Create, change, enable and delete workflows",
    "workflow.run": "Start workflows by hand; the workflow's steps still need their approvals",
    "workflow.approve": "Approve or reject the steps of a workflow that wait for a person",
}

ROLE_NAME_PATTERN = r"^[a-z][a-z0-9-]{1,31}$"


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    ldap_group: str
    builtin: bool
    permissions: frozenset[str]


async def list_roles(conn: AsyncConnection) -> list[Role]:
    rows = await conn.execute(
        text(
            "SELECT r.name, r.description, r.ldap_group, r.builtin,"
            " coalesce(array_agg(p.permission) FILTER (WHERE p.permission IS NOT NULL), '{}')"
            " AS permissions"
            " FROM role r LEFT JOIN role_permission p ON p.role = r.name"
            " GROUP BY r.name ORDER BY r.builtin DESC, r.name"
        )
    )
    return [
        Role(
            name=r.name,
            description=r.description,
            ldap_group=r.ldap_group,
            builtin=r.builtin,
            permissions=frozenset(PERMISSIONS)
            if r.name == ADMIN_ROLE
            else frozenset(p for p in r.permissions if p in PERMISSIONS),
        )
        for r in rows
    ]


async def roles_for_groups(conn: AsyncConnection, groups: frozenset[str]) -> list[str]:
    """Roles whose LDAP group CN is among the user's groups (case-insensitive)."""
    if not groups:
        return []
    rows = await conn.execute(
        text("SELECT name FROM role WHERE lower(ldap_group) = ANY(:groups) ORDER BY name"),
        {"groups": sorted(g.lower() for g in groups)},
    )
    return [r.name for r in rows]


async def permissions_for(conn: AsyncConnection, roles: tuple[str, ...]) -> frozenset[str]:
    if ADMIN_ROLE in roles:
        return frozenset(PERMISSIONS)
    if not roles:
        return frozenset()
    rows = await conn.execute(
        text("SELECT DISTINCT permission FROM role_permission WHERE role = ANY(:roles)"),
        {"roles": list(roles)},
    )
    return frozenset(r.permission for r in rows if r.permission in PERMISSIONS)
