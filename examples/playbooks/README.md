# Example playbooks

Two read-only playbooks to try the Ansible console (Janus-Ops › Ansible).

- `uptime.yml`: uptime and load of each host.
- `updates.yml`: pending package updates (Debian family: simulated upgrade; RHEL family: `dnf check-update`). Nothing is installed.

Import them with a source of kind **Upload** (drop the folder as a `.zip` or `.tar.gz`), or
push this folder to a Git repository and use a **Git** source.

Both playbooks run with `dry run` on by default; they change nothing on the nodes.
