L’architettura di **Janus-Ops** si regge su un principio fondamentale: **un unico Inventory centrale attorno a cui ruotano moduli disaccoppiati**, alimentato da un micro-agent auto-contenuto progettato per sopravvivere anche in reti segmentate, DMZ o dietro proxy HTTP/HTTPS restrittivi.

Di seguito trovi la scomposizione completa in macro-blocchi, con specifiche funzionali dettagliate, tecnologie e protocolli per ciascun componente.

---

## 1. Il Micro-Agent: `janus-agent` (Zero-Dependency & Proxy-Ready)

Per evitare problemi di dipendenze Python/pip, librerie condivise (`glibc` mismatch tra CentOS/Debian/Alpine) o blocchi firewall su reti isolate, l'agent non va scritto in Python ma compilato come **singolo binario statico** (o pacchetto bundle) inviato direttamente dal server Janus-Ops via SSH durante il bootstrap.

* **Tecnologia Consigliata:** **Go (Golang)** compilato con `CGO_ENABLED=0` (produce un singolo eseguibile statico da ~12-18 MB per `linux/amd64`, `linux/arm64`, `windows/amd64` senza alcuna dipendenza esterna).
* **Meccanismo di Deploy (Push dal Server):**
* Il server Janus-Ops conserva nella directory `/opt/Janus-Ops/agent-bundles/` i binari precompilati per le varie architetture.
* Al momento dell'aggiunta di un host, il backend usa `asyncssh` per rilevare OS e architettura (`uname -m`), copia il binario via SFTP in `/usr/local/bin/janus-agent`, genera il file di configurazione `/etc/janus-agent/config.yaml` (con token univoco e certificato CA del server) e registra/avvia il servizio `systemd` (o OpenRC/Windows Service).
* **Auto-Update Air-Gapped:** Quando aggiorni Janus-Ops, il server notifica gli agent connessi che scaricano il nuovo binario direttamente dall'endpoint interno del server Janus-Ops (`/api/v1/agent/binary/{arch}`), senza mai toccare internet.


* **Architettura di Rete (Outbound-Only per Proxy Stretti):**
* L'agent **non apre porte in ascolto** sul nodo gestito. È sempre l'agent a iniziare una connessione persistente verso il server Janus-Ops tramite **gRPC over HTTP/2** o **WebSocket sicuro (`wss://`)** sulla porta `443`.
* Supporto nativo per `HTTP_PROXY` / `HTTPS_PROXY` (inclusa autenticazione Basic/NTLM e certificati CA aziendali custom) e riconnessione automatica con exponential backoff e buffer locale su disco (SQLite/bbolt) per non perdere metriche in caso di disconnessione temporanea.


* **Funzionalità Integrate nell'Agent:**
* **Telemetria di Sistema (Checkmk-grade):** Raccolta ad alta frequenza (ogni 5-15s) di CPU (per core, steal, iowait), RAM/Swap, Disk I/O, spazio filesystem, stato `systemd` units, porte in ascolto, temperature hardware/SMART e processi top.
* **Local Docker Proxy (Stile Hawser):** Se abilitato, l'agent parla localmente con `/var/run/docker.sock` e fa da ponte sicuro verso il server Janus-Ops, eliminando la necessità di esporre la porta TCP 2375 o aprire tunnel SSH multipli per gestire container, log e stats.
* **Remote Command / Task Executor:** Esecuzione controllata di script, health-check custom (stile plugin locali Checkmk) e sessioni PTY (terminale interattivo) incapsulate nello stesso tunnel `wss://` (utile se l'SSH diretto verso il nodo è bloccato dal firewall).



---

## 2. Core Platform: Inventory Unificato, Auth & Secret Vault

È il cervello relazionale che collega ogni indirizzo IP o FQDN a tutte le sue capacità operative.

* **Tecnologie:** **Python 3.12+**, **FastAPI**, **SQLAlchemy 2.0 (async)**, **PostgreSQL 16 + TimescaleDB**, **Redis**.
* **Funzionalità:**
* **Unified Host Inventory:** Ogni record `Node` ha attributi dinamici (Tags, Gruppi, Ambiente `prod/lab/dmz`) e *Capabilities* attivabili: `[x] Janus Agent`, `[x] SSH Target`, `[x] Proxmox Node`, `[x] Docker Host`, `[x] SNMP Device`.
* **Secret & Credential Vault:** Archivio cifrato (AES-256-GCM / Fernet con master key derivata o integrazione opzionale con **OpenBao / HashiCorp Vault**) per chiavi private SSH, password `sudo`, API Token Proxmox, token registry Docker e credenziali SNMPv3.
* **RBAC & Audit Log Immutabile:** Gestione ruoli (Admin, Operator, Read-Only Auditor) e tracciamento completo di chi ha aperto una shell SSH, chi ha riavviato un container o lanciato un playbook Ansible (con registrazione opzionale delle sessioni terminale in formato asciinema).



---

## 3. Modulo Observability & Alerting (Checkmk Engine)

Replica la precisione diagnostica e la densità informativa di Checkmk, superandone la rigidità grafica.

* **Tecnologie:** **TimescaleDB** (hypertables per serie temporali ad alte prestazioni), **Taskiq / Celery** (scheduler distribuito), **pysnmp** (per NAS, switch, UPS, stampanti), **ECharts** (per grafici esagonali di stato e serie storiche zoomabili).


* **Funzionalità:**
* **Service Discovery Automatico:** Quando aggiungi un host (via agent o SNMP), Janus-Ops rileva automaticamente dischi, interfacce di rete, servizi `systemd` critici, pool ZFS, container Docker e VM Proxmox, proponendo i check da attivare con un clic.
* **Soglie Multi-Livello & Hysteresis:** Stati `OK`, `WARN`, `CRIT`, `UNKNOWN` con finestre di tolleranza (es. "allerta solo se CPU > 90% per 3 cicli consecutivi") per evitare falsi positivi.


* **Gestione Operativa degli Incidenti:** Funzioni stile NOC come *Acknowledge* (presa in carico con commento), *Scheduled Downtime* (manutenzione programmata che silenzia gli alert), *Flapping Detection* e *Parent/Child Dependencies* (se cade uno switch o un nodo Proxmox, sopprime gli alert delle 20 VM figlie segnando solo il nodo padre come `DOWN`).
* **Notification Router:** Invio alert via Telegram, Webhook, Email, Slack/Discord con regole di escalation.



---

## 4. Modulo Container & Stack Manager (Dockhand Engine)

Gestione completa del ciclo di vita Docker su singoli host o cluster multi-nodo.

* **Tecnologie:** **Docker SDK for Python (`aiodocker`)**, comunicante tramite il tunnel dell'agent `janus-agent` oppure via `ssh://` tunnel nativo.
* **Funzionalità:**
* **Vista Multi-Environment:** Dashboard a card per singolo host Docker (conteggio container running/stopped/unhealthy, uso risorse, eventi recenti) e vista aggregata globale di tutti i container dell'infrastruttura.


* **Gestione Container Avanzata:** Start/Stop/Restart/Kill, ispezione JSON completa, live stats (CPU/RAM/Net/Block I/O), streaming dei log in tempo reale con ricerca/filtro regex, e **Exec Shell** interattiva nel browser.
* **Compose Stacks & GitOps:** Editor YAML integrato (Monaco Editor con validazione schema `docker-compose`), gestione variabili `.env` cifrate, deploy/redeploy di stack, e sincronizzazione diretta da repository Git (GitHub/Gitea self-hosted) con webhook auto-deploy.
* **Manutenzione & Registry:** Gestione Volumi (con browser file/backup), Reti, Pulizia intelligente (*Prune* selettivo di immagini dangling/volumi orfani) e controllo aggiornamenti immagini sui registry (Docker Hub, GHCR, registry privati).



---

## 5. Modulo Virtualizzazione & Cluster (Proxmox VE Manager)

Controllo centralizzato di uno o più cluster Proxmox VE e nodi standalone senza dover saltare tra diverse interfacce web.

* **Tecnologie:** **Proxmoxer** (wrapper Python asincrono sulle API REST ufficiali Proxmox), **noVNC / xterm.js** (per console grafiche e seriali).
* **Funzionalità:**
* **Cluster & Node Overview:** Stato quorum, carico nodi, utilizzo storage condivisi (NFS, Ceph, ZFS local), temperatura e stato dischi.
* **Ciclo di Vita VM & LXC:** Creazione rapida da template Cloud-Init, Start, Shutdown, Reboot, Snapshot, Clone, Migrazione live tra nodi del cluster e ridimensionamento risorse al volo.
* **Console Integrata:** Accesso diretto alla console seriale LXC/VM tramite `xterm.js` o console grafica VNC/SPICE direttamente dentro una tab di Janus-Ops.
* **Backup & Replication Audit:** Monitoraggio dedicato sullo stato dei job `vzdump` / Proxmox Backup Server (PBS) — esattamente come mostrato nella tua dashboard Checkmk — con evidenza immediata di backup mancanti, falliti o troppo vecchi.





---

## 6. Modulo SSH Connection Manager & Bastion Terminal

Un sostituto web-based di MobaXterm / Termius integrato nell'inventory.

* **Tecnologie:** **AsyncSSH** (backend), **xterm.js** + WebSockets (frontend).
* **Funzionalità:**
* **Accesso 1-Click & Multi-Tab:** Apertura istantanea di sessioni SSH su qualsiasi nodo dell'inventory (usando le credenziali salvate nel Vault, senza mai esporre chiavi private al browser) in tab affiancate o split-screen.
* **SFTP File Browser Integrato:** Pannello laterale per navigare il filesystem remoto, caricare/scaricare file drag-and-drop e modificare file di configurazione (`/etc/...`) direttamente con l'editor del portale.
* **Command Broadcast & Snippet Library:** Possibilità di inviare lo stesso comando contemporaneamente a 5 terminali aperti (es. durante un aggiornamento manuale) e libreria condivisa di comandi rapidi parametrizzati.
* **ProxyJump / Bastion Trasparente:** Se un host è raggiungibile solo tramite un jump host o tramite il tunnel inverso di `janus-agent`, Janus-Ops instrada la sessione in automatico.



---

## 7. Modulo Automazione: Ansible & Visual Workflow Engine

Il motore operativo che trasforma gli eventi di monitoraggio o le richieste dell'utente in azioni concrete.

* **Tecnologie:** **ansible-runner** (per esecuzione asincrona di playbook con streaming degli eventi JSON), **React Flow** (frontend per il builder visuale a nodi), **Temporal** o **Celery Canvas** (orchestrazione workflow).
* **Funzionalità:**
* **Ansible Control Center:**
* Generazione dinamica dell'inventory Ansible direttamente dal database di Janus-Ops (gruppi, tag e variabili host sempre sincronizzati).
* Catalogo Playbook (importati da Git o creati nell'editor), form dinamici generati dalle variabili (`survey`), esecuzione con output live formattato per singolo task/host e storico esecuzioni.


* **Visual Workflow Manager (Event-Driven & Scheduled):**
* Editor a nodi stile n8n/Node-RED progettato per operazioni sistemistiche.
* **Trigger:** Cron schedule, Webhook, Pulsante manuale, oppure **Alert di Monitoring** (es. *Se il disco `/var/log` su un nodo supera il 90%*).
* **Nodi Azione:** Esegui comando SSH, Lancia Playbook Ansible, Riavvia Container Docker, Crea Snapshot Proxmox, Chiedi analisi all'AI, Attendi approvazione umana (Human-in-the-loop), Invia notifica Telegram.





---

## 8. Modulo Janus AI Copilot (Troubleshooting & Infrastructure-as-Code)

Un assistente operativo contestuale che conosce la topologia reale della tua infrastruttura.

* **Tecnologie:** **LiteLLM / OpenAI-compatible SDK** (connesso ai tuoi endpoint locali Ollama / vLLM / SGLang o API cloud), **Tool Calling / MCP (Model Context Protocol)** interno in sola lettura di default.
* **Funzionalità:**
* **Root Cause Analysis (RCA) su Alert:** Cliccando *"Diagnose"* su un servizio `CRIT` nella dashboard, l'AI raccoglie automaticamente gli ultimi log di sistema (`journalctl`), i log del container, le metriche recenti e le ultime modifiche registrate nell'audit log, restituendo una diagnosi precisa e il comando/playbook consigliato per risolvere.


* **Generatore IaC Contestuale:** Generazione assistita di `docker-compose.yml`, playbook Ansible, unit `systemd` o filtri regex, già calibrati sugli IP, percorsi storage e convenzioni del tuo inventory.
* **Safe Execution Guardrails:** L'AI **non esegue mai** comandi distruttivi in autonomia; genera un *Action Plan* (es. un playbook o una sequenza di comandi) che l'amministratore può rivedere, modificare e approvare con un clic.



---

## 9. Frontend Architecture (Mix Checkmk + Dockhand)

* **Tecnologie:** **Next.js 15 (App Router)**, **TypeScript**, **Tailwind CSS**, **Shadcn UI**, **TanStack Query + Zustand** (gestione stato real-time via WebSocket), **ECharts** (grafici NOC), **Monaco Editor** (YAML/JSON/Bash), **xterm.js** (terminale), **React Flow** (workflow).
* **Struttura UI:**
* **Command Palette Globale (`Ctrl+K`)**: Navigazione istantanea ("vai a pve01", "apri ssh su docker2", "lancia backup check", "chiedi all'AI perché nginx è down").


* **NOC Dashboard Ibrida:**
* In alto: Grafici esagonali Checkmk-style (Host & Service stats) + Feed eventi real-time.


* Al centro: Griglia di Node Cards stile Dockhand con barre CPU/RAM, stato container/VM e pulsanti rapidi (`SSH`, `Ansible`, `AI`).


* In basso: Tabella densa degli *Unhandled Problems* con badge ad alto contrasto (`CRIT`, `WARN`, `DOWN`) e tasto rapido di diagnosi AI.






 