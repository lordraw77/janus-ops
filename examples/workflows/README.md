# Workflow recipes

Definitions you can paste into the **JSON** tab of the workflow editor (**Workflows → New workflow → JSON**), then
open the **Form** or **Graph** tab and pick your own node, channel and Proxmox host: the ids in these files are
placeholders (`...00a` is a node, `...00c` a notification channel).

| File | What it does |
| --- | --- |
| [`restart-service-on-alert.json`](restart-service-on-alert.json) | When a check raises a problem: if the service is down, restart it without waiting for a person, wait, check the page answers (three tries), and say how it went. |
| [`container-watchdog.json`](container-watchdog.json) | Every 5 minutes: if the container is not running, restart it and tell the channel. |
| [`snapshot-then-upgrade.json`](snapshot-then-upgrade.json) | A maintenance window: a person approves, a Proxmox snapshot is taken, the guest is upgraded and rebooted, and the workflow waits for SSH to come back. |
| [`stop-runaway-process.json`](stop-runaway-process.json) | Ask a process to stop (TERM); if it is still there ten seconds later, kill it. |

The steps that change something (`restart`, `snapshot`, `kill`, `reboot`, a command) wait for a person unless the step
has `"unattended": true`, as the first two recipes do on purpose: that choice is yours, and every run is recorded in the
audit log either way. A `{{step.output}}` in a message is replaced by the output of that earlier step.
