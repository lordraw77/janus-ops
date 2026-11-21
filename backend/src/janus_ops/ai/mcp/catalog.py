"""Which MCP tools the Copilot may use, and how. An allow-list: a tool that is in neither set is
never offered, so a tool added by a new server version stays out until someone classifies it
(the integration test lists the live tools and fails on an unclassified one).

`read` tools only look. `write` tools change a host (restart, power, delete, run a command) and need
the `ai.operate` permission of the user who started the run.
"""

from typing import Literal

Mode = Literal["read", "write"]


def _names(text: str) -> frozenset[str]:
    return frozenset(text.split())


LINUX_READ = _names(
    "list_servers read_file get_file_stat list_directory search_files grep_files "
    " get_system_info get_network_info check_port list_processes list_services tail_log "
    " grep_logs docker_ps docker_logs list_packages watch_metrics top_processes "
    " firewall_rules ping_host traceroute_host list_users list_groups disk_usage "
    " list_mounts list_crontabs check_cert git_status git_log"
)
LINUX_WRITE = _names(
    "execute_command write_file delete_path create_directory upload_file download_file "
    " kill_process service_control docker_control package_control firewall_control "
    " user_control mount_control add_cron_job remove_cron_job git_pull nginx_control "
    " apache_control broadcast_command"
)
# Not offered even though classified: they make no sense with one host per run.
LINUX_HIDDEN = frozenset({"list_servers", "broadcast_command"})

PROXMOX_READ = _names(
    "list_nodes list_vms list_storage vm_status vm_config cluster_resources cluster_tasks "
    " node_tasks list_snapshots storage_content node_network list_backups "
    " list_firewall_rules list_firewall_aliases list_firewall_ipsets node_rrddata "
    " vm_rrddata cluster_status ha_resources ha_groups node_apt_updates node_syslog "
    " node_dns node_subscription list_users list_tokens list_acl list_pools list_vnets "
    " list_sdn_zones vm_agent_info vm_agent_network list_node_disks list_backup_jobs "
    " list_replication ceph_status ceph_health ceph_osds ceph_pools "
    " list_notification_endpoints list_notification_matchers storage_status node_smart "
    " cluster_health_summary node_top list_acme_accounts wait_for_task node_certificates"
)
PROXMOX_WRITE = _names(
    "create_backup restore_backup vm_clone vm_create vm_delete vm_resize_disk "
    " create_firewall_rule delete_firewall_rule vm_start vm_stop vm_shutdown vm_reboot "
    " create_snapshot delete_snapshot rollback_snapshot vm_migrate vm_move_disk "
    " vm_unlink_disk vm_template vm_agent_exec prune_backups create_replication "
    " delete_replication node_reboot node_shutdown node_apt_upgrade vm_console_url "
    " cancel_task vm_set_config vm_set_cdrom lxc_exec create_storage delete_storage "
    " create_pool delete_pool pool_add_member pool_remove_member create_network "
    " delete_network apply_network_config renew_certificate create_api_token "
    " delete_api_token"
)

SERVERS = {
    "linux": (LINUX_READ, LINUX_WRITE, LINUX_HIDDEN, "linux"),
    "proxmox": (PROXMOX_READ, PROXMOX_WRITE, frozenset(), "pve"),
}


def classify(server: str, tool: str) -> Mode | None:
    read, write, hidden, _ = SERVERS[server]
    if tool in hidden:
        return None
    if tool in read:
        return "read"
    if tool in write:
        return "write"
    return None


def known(server: str) -> frozenset[str]:
    read, write, _, _ = SERVERS[server]
    return read | write


def prefix(server: str) -> str:
    return SERVERS[server][3]
