# Janus-Ops — Analisi & Piano di Implementazione

> Documento di pianificazione derivato da [specs.md](specs.md).
> Stato: **bozza v0.2 — 2026-11-13**. Le decisioni marcate `OPEN` attendono conferma.
>
> **v0.2** — integrate le risposte alle domande aperte: scala confermata (A2), LDAP come directory
> sidecar nello stack (A5 → ADR-008), Windows fuori dall'MVP (A4), AI su modelli self-hosted esterni
> con accesso a tutti i dati operativi (A6), licenza TimescaleDB verificata (C12). Aggiunta analisi del
> `janusops-design-kit/` (§8).
>
> **v0.3** — licenza **Apache-2.0** (A8), grafia del brand **Janus-Ops** (C15), favicon e varianti del
> logo generate dal master, `res/` come unica cartella degli asset di design (§8). Nessuna domanda aperta.
>
> **v0.4** — avvio F0: immagine unica `lordraw77/janus-ops` (ADR-009, AMEND ADR-006), schema DDL
> F0–F3 come migrazione Alembic `0001`, scaffold backend/frontend/deploy/CI, overview Docker Hub
> (`docs/dockerhub/`). Avanzamento in §5.1.
>
> **v0.5** — login LDAP con username e password, ruoli dai gruppi, sessioni, break-glass, audit con
> hash chain, pagina di login (AC-02, AC-02b, AC-02c verificati). Dettagli implementativi in ADR-008.
>
> **v0.6** — amministratore iniziale da `.env`; gestione utenti dalla UI (lldap via `janus-manager`);
> ruoli configurabili con permessi (ADR-010), incluso chi legge l'audit log; viewer dell'audit;
> **TOTP opzionale** per utente, funzionante dietro proxy aziendale e air-gapped.

**Tier: L** — sistema multi-servizio (backend, worker, frontend, agent Go), integrazioni esterne
(SSH, Docker, Proxmox, SNMP, LLM), requisiti di sicurezza elevati (esecuzione remota di comandi).

---

## ASSUMPTIONS

Ogni assunzione è versionata. Se viene invalidata, la colonna "Impatta" indica cosa rivedere.

| ID | Assunzione | Impatta |
|---|---|---|
| A1 | Deploy **self-hosted, single-tenant** (una organizzazione per installazione) | Data model, RBAC, ADR-001 |
| A2 | ✅ *confermata "per il momento"* — Scala MVP: **≤ 500 nodi**, **≤ 20 utenti concorrenti**, un singolo server Janus-Ops (no HA nell'MVP) | ADR-002, ADR-005, sizing Timescale |
| A3 | Runtime target: **Docker Compose** su un host Linux; k8s non nell'MVP | DevOps, deploy/ |
| A4 | ✅ *confermata* — Agent MVP solo **Linux amd64/arm64** con systemd; Windows e OpenRC rimandati a F8+ | Agent, bootstrap |
| A5 | ✅ *rivista v0.2* — Autenticazione MVP: **directory LDAP come container sidecar nello stack** (utenti e gruppi), ruoli Janus mappati sui gruppi LDAP, **TOTP gestito da Janus-Ops, opzionale per utente** (*rivista v0.6*), un account locale *break-glass* per l'emergenza. Collegamento ad AD/LDAP aziendale esterno: SHOULD successivo. Vedi ADR-008 | Auth, F0, deploy |
| A6 | ✅ *rivista v0.2* — LLM: modelli **self-hosted su host esterni allo stack** (Ollama / vLLM / SGLang), endpoint OpenAI-compatible configurabile; **nessun provider cloud**. All'AI è consentito l'accesso a **tutti i dati operativi** (log, metriche, audit, inventory). Restano esclusi solo i segreti del vault | AI, sicurezza |
| A7 | Il server Janus-Ops può raggiungere direttamente (o via jump host) le API Proxmox e gli host SSH | F1, F5 |
| A8 | ✅ *decisa v0.3* — Janus-Ops è rilasciato sotto **Apache-2.0** (`LICENSE`, `NOTICE`). Le dipendenze devono avere una licenza compatibile; per TimescaleDB vedi C12. Ansible (GPLv3) è invocato come processo esterno via ansible-runner (Apache-2.0), non incluso nel codice di Janus-Ops | Supply chain |

---

## 1. Analisi critica delle specifiche

Punti delle specifiche che vanno corretti, chiariti o ridimensionati prima di scrivere codice.

| # | Punto in specs.md | Problema | Proposta |
|---|---|---|---|
| C1 | Scope generale (8 moduli) | Equivale a ricostruire Checkmk + Portainer + PVE UI + Termius + AWX + n8n + Copilot. Senza fasi rigide il progetto non arriva mai a un MVP utilizzabile. | Roadmap a fasi incrementali, ognuna rilasciabile (§5). Ogni modulo parte con un sottoinsieme MUST. |
| C2 | Agent: "gRPC over HTTP/2 **o** WebSocket" | Due protocolli = doppio costo. gRPC/HTTP2 viene spesso rotto dai proxy aziendali con TLS inspection. | **Solo WSS** su 443 con multiplexing di stream (ADR-002). |
| C3 | Scheduler: "Taskiq / Celery" + workflow "Temporal o Celery Canvas" | Tre motori candidati per due esigenze. Temporal aggiunge un cluster da gestire. Celery non è nativo async. | Un solo motore async per task + workflow engine interno persistito su Postgres (ADR-003). Temporal rivalutato solo se servono workflow di lunga durata complessi. |
| C4 | "Proxmoxer (wrapper Python **asincrono**)" | Proxmoxer è sincrono `[VERIFY: supporto async nelle versioni correnti]`. | Client HTTP async (httpx) sottile sulle API REST PVE, oppure proxmoxer in thread pool. Decisione in F5. |
| C5 | Docker "via tunnel agent **oppure** `ssh://`" con aiodocker | aiodocker non gestisce `ssh://` nativamente `[VERIFY]`. | L'agent espone la Docker Engine API come stream nel tunnel; il backend parla HTTP Docker su quello stream. `ssh://` solo come fallback futuro. |
| C6 | Proxy con autenticazione **NTLM** | Non supportato dalla stdlib Go; richiede libreria terza `[VERIFY: libreria mantenuta]`. | MVP: proxy senza auth + Basic + CA custom. NTLM come SHOULD in fase successiva. |
| C7 | Agent: token + certificato CA | Un token statico in chiaro su disco è il solo segreto di autenticazione. | Enrollment con token **monouso** → emissione certificato client per agent (**mTLS**), rotazione periodica. |
| C8 | Auto-update agent | Distribuire binari a tutta la flotta è un vettore di compromissione di massa. | Binari **firmati** (ed25519) con chiave di release separata dal server; l'agent verifica firma e checksum prima di sostituirsi. |
| C9 | "Remote Command / Task Executor" nell'agent | Di fatto RCE by design: il server Janus-Ops diventa il bersaglio più critico dell'infrastruttura. | Policy locale sull'agent (`allow_exec`, `allow_pty`, `allow_docker`) che il server non può sovrascrivere; tutto auditato. |
| C10 | AI RCA su log | I log sono input non fidato → **prompt injection** (un log può contenere istruzioni). | Tool AI read-only; output mai eseguito senza approvazione; i segreti del vault non sono mai esposti ai tool AI; traffico verso gli endpoint LLM esterni solo su TLS (A6). |
| C11 | "Audit Log Immutabile" | Una tabella SQL non è immutabile per un admin DB. | Append-only (no UPDATE/DELETE via grant) + hash chain per evidenziare manomissioni; export esterno opzionale. |
| C12 | TimescaleDB | ✅ *verificato v0.2*: due edizioni. **Apache 2 Edition** (core, hypertable) e **Community Edition** sotto **Timescale License (TSL)**, che include compressione e continuous aggregates. La TSL è gratuita per uso self-hosted, anche commerciale; vieta di offrire TimescaleDB come servizio database gestito. | Usare la Community Edition (immagine ufficiale), che è il default (`SHOW timescaledb.license;` → `timescale`). Janus-Ops usa il DB via SQL senza modificarne il codice e non lo vende come DBaaS, quindi la TSL non vincola la licenza di Janus-Ops. |
| C13 | Next.js "15" | Versione da verificare al momento del bootstrap `[VERIFY: ultima stable]`. Tailwind: i file in `res/` usano `@import "tailwindcss"` → **Tailwind v4**. | Fissare le versioni in F0. |
| C14 | File in `res/` | ✅ *risolto v0.2*: i file sono stati rinominati. | — |
| C15 | Nome del brand | ✅ *risolto v0.3*: grafia ufficiale **Janus-Ops**. Nel wordmark della UI: "Janus-" in `--text-primary`, "Ops" in `--accent-primary`. Per identificatori tecnici: `janus-ops` / `janus-agent`. | — |

---

## 2. Product Owner — Requisiti

### 2.1 Requisiti funzionali (MVP = F0–F3, poi per fase)

| ID | Requisito | Priorità | Fase |
|---|---|---|---|
| FR-01 | Inventory unificato di nodi con tag, gruppi, ambiente (`prod/lab/dmz`) e capability attivabili | MUST | F0 |
| FR-02 | Vault cifrato per chiavi SSH, password sudo, token API | MUST | F0 |
| FR-03 | Login con utenti della directory LDAP sidecar, ruoli mappati sui gruppi LDAP. **AMEND Product Owner (v0.6)**: TOTP **opzionale** per utente (era obbligatorio), su richiesta dell'utente | MUST | F0 |
| FR-08 | Amministratore iniziale creato da `.env` al primo avvio, solo se assente | MUST | F0 |
| FR-09 | Gestione utenti dalla UI: crea, modifica, ruoli, disabilita, reset password, reset 2FA, elimina | MUST | F0 |
| FR-10b | Ruoli configurabili: gruppo LDAP + insieme di permessi; ruoli custom; chi legge l'audit log è un permesso (`audit.read`) | MUST | F0 |
| FR-11b | TOTP funzionante senza accesso a internet (proxy aziendali, air-gapped) | MUST | F0 |
| FR-06 | Account locale *break-glass*, usabile anche con LDAP non disponibile, ogni uso segnalato nell'audit | MUST | F0 |
| FR-07 | Collegamento opzionale ad AD/LDAP aziendale esterno al posto del sidecar | SHOULD | F8 |
| FR-04 | Audit log di ogni azione privilegiata, consultabile e filtrabile | MUST | F0 |
| FR-05 | Command palette `Ctrl+K` per navigare su nodi e azioni | SHOULD | F0 |
| FR-10 | Sessione SSH web 1-click su un nodo, con credenziali dal vault | MUST | F1 |
| FR-11 | Multi-tab / split, SFTP browser, upload/download | SHOULD | F1 |
| FR-12 | Broadcast comando su più terminali, snippet library | SHOULD | F1 |
| FR-13 | Registrazione sessione (asciinema) | SHOULD | F1 |
| FR-14 | ProxyJump tramite jump host | SHOULD | F1 |
| FR-20 | Bootstrap agent via SSH (detect arch, copia binario, config, systemd) | MUST | F2 |
| FR-21 | Agent connesso in uscita via WSS, proxy HTTP(S) con CA custom | MUST | F2 |
| FR-22 | Telemetria ogni 5–15 s: CPU, RAM/swap, disk I/O, filesystem, unit systemd, porte, top processi | MUST | F2 |
| FR-23 | Buffer locale su disco durante disconnessione e invio differito | MUST | F2 |
| FR-24 | NOC dashboard: hexagon widget, node cards, tabella unhandled problems | MUST | F2 |
| FR-30 | Stati OK/WARN/CRIT/UNKNOWN con soglie e isteresi (N cicli) | MUST | F3 |
| FR-31 | Acknowledge, scheduled downtime | MUST | F3 |
| FR-32 | Dipendenze parent/child con soppressione alert figli | MUST | F3 |
| FR-33 | Flapping detection | SHOULD | F3 |
| FR-34 | Notifiche Telegram, Webhook, Email; Slack/Discord | MUST (primi 3) / SHOULD | F3 |
| FR-35 | Service discovery automatico con proposta check | SHOULD | F3 |
| FR-40 | Docker: lista container per host, start/stop/restart/kill, inspect, log streaming, stats | MUST | F4 |
| FR-41 | Exec shell nel container dal browser | MUST | F4 |
| FR-42 | Compose stack: editor YAML, `.env` cifrati, deploy/redeploy | SHOULD | F4 |
| FR-43 | Volumi, reti, prune selettivo, check aggiornamenti immagini | SHOULD | F4 |
| FR-50 | Proxmox: stato cluster/quorum, nodi, storage | MUST | F5 |
| FR-51 | VM/LXC: start/shutdown/reboot/snapshot/clone/migrazione | MUST | F5 |
| FR-52 | Console VM/LXC (xterm.js seriale; noVNC) | SHOULD | F5 |
| FR-53 | Audit backup vzdump/PBS: mancanti, falliti, troppo vecchi | MUST | F5 |
| FR-60 | Inventory Ansible dinamico dal DB | MUST | F6 |
| FR-61 | Catalogo playbook (Git), survey, esecuzione con output live, storico | MUST | F6 |
| FR-70 | AI "Diagnose" su servizio CRIT: raccolta log/metriche/audit → diagnosi + action plan | MUST | F7 |
| FR-71 | Action plan approvabile/modificabile prima dell'esecuzione | MUST | F7 |
| FR-72 | Generatore IaC contestuale | SHOULD | F7 |
| FR-80 | Workflow visuale (trigger cron/webhook/manuale/alert; azioni SSH/Ansible/Docker/Proxmox/AI/approvazione/notifica) | SHOULD | F8 |
| FR-81 | GitOps compose con webhook auto-deploy | SHOULD | F8 |
| FR-82 | SNMP (v2c/v3) per NAS, switch, UPS | SHOULD | F8 |
| FR-83 | PTY e comandi remoti via tunnel agent | SHOULD | F8 |
| FR-84 | Auto-update agent air-gapped firmato | SHOULD | F8 |
| FR-85 | Agent Windows | WON'T (MVP) | F9+ |
| FR-86 | Integrazione OpenBao/Vault esterno | WON'T (MVP) | F9+ |
| FR-87 | Multi-tenancy, HA del server | WON'T (MVP) | — |

### 2.2 Requisiti non funzionali

| ID | Requisito | Verifica |
|---|---|---|
| NFR-01 | Agent: singolo binario statico, nessuna dipendenza runtime, nessuna porta in ascolto | `ldd` → "not a dynamic executable"; `ss -ltnp` sul nodo non mostra l'agent |
| NFR-02 | Agent funzionante dietro proxy HTTP(S) con CA custom | Test E2E con proxy (es. squid) in compose di test |
| NFR-03 | Nessuna perdita di metriche per disconnessioni ≤ 1 h | Test: disconnessione 60 min → gap assente nei dati |
| NFR-04 | Latenza ingest → stato in dashboard ≤ 30 s (p95) | Misura end-to-end in test di carico |
| NFR-05 | Ingest sostenuto per 500 nodi a 10 s (~10k punti/s stimati) | Test di carico sintetico |
| NFR-06 | Segreti mai inviati al browser, mai nei log | Test automatici su response/log + review |
| NFR-07 | Ogni azione privilegiata produce un evento di audit attribuito | Test per ogni endpoint mutante |
| NFR-08 | Installazione da clone pulito con soli comandi documentati | Job CI "fresh install" |
| NFR-09 | UI usabile ≥ 1280 px; dashboard leggibile a 1920 px (NOC wall) | Review visuale / Playwright screenshot |

### 2.3 Criteri di accettazione MVP (estratto)

| ID | Criterio (osservabile dall'esterno) | Metodo |
|---|---|---|
| AC-01 | Un Operator aggiunge un host con IP + credenziale SSH e vede la capability "SSH Target" attiva | E2E Playwright |
| AC-02 | Un Auditor non può aprire shell né modificare nodi (HTTP 403, pulsanti assenti) | API test + E2E |
| AC-02b | Un utente aggiunto al gruppo LDAP `janus-operators` ottiene il ruolo Operator al login successivo; rimosso dal gruppo, lo perde | Integration test con container LDAP |
| AC-02c | Con il container LDAP fermo, solo l'account break-glass può accedere e l'evento compare nell'audit | Integration test |
| AC-03 | Aprendo una shell, l'audit log registra utente, nodo, timestamp inizio/fine | API test |
| AC-04 | Bootstrap agent su Debian e Rocky (amd64/arm64): l'host diventa "online" entro 60 s | E2E su VM/container di test |
| AC-05 | Bloccando l'uscita per 10 min, al ripristino la serie temporale è continua | Integration test |
| AC-06 | CPU > soglia CRIT per 3 cicli → stato CRIT e notifica webhook; per 2 cicli → nessun alert | Integration test |
| AC-07 | Con nodo padre DOWN, i figli sono "UNREACHABLE" e non generano notifiche | Integration test |
| AC-08 | In downtime programmato nessuna notifica viene inviata | Integration test |

---

## 3. Architect — Componenti, flussi, ADR

### 3.1 Componenti

```mermaid
flowchart TB
    subgraph Edge
        RP["Reverse proxy<br/>TLS termination :443"]
    end
    subgraph App
        FE["frontend<br/>Next.js"]
        API["api<br/>FastAPI REST + WS"]
        GW["agent-gateway<br/>WSS mTLS, multiplexing"]
        WK["worker<br/>task queue consumers"]
        SCH["scheduler<br/>cron, check timers"]
    end
    subgraph Data
        PG[("PostgreSQL + TimescaleDB")]
        RD[("Redis<br/>broker, pub/sub, cache")]
        LD[("LDAP directory<br/>sidecar")]
    end
    AG["janus-agent"]
    LLM["LLM self-hosted<br/>(host esterni)"]

    RP --> FE
    RP --> API
    RP --> GW
    AG -->|WSS| RP
    API --> PG
    API --> RD
    GW --> RD
    GW --> PG
    WK --> PG
    WK --> RD
    SCH --> RD
    API --> LD
    WK -->|TLS| LLM
```

| Componente | Responsabilità | Requisiti coperti |
|---|---|---|
| reverse proxy | TLS, routing per path, unico ingresso :443 | NFR-01, NFR-02 |
| frontend | UI, stato real-time via WS | FR-05, FR-24, tutte le viste |
| api | Auth, RBAC, inventory, vault, audit, REST, WS verso browser, SSH bastion | FR-01..14, FR-40..72 (lato API) |
| agent-gateway | Terminazione tunnel agent, enrollment, routing stream (docker/exec/pty), ingest metriche in batch | FR-20..23, FR-40, FR-83 |
| worker | Valutazione check, alerting, notifiche, Ansible, Proxmox, AI, workflow | FR-30..35, FR-50..53, FR-60..80 |
| scheduler | Timer di check, cron workflow, retention | FR-30, FR-80 |
| ldap (sidecar) | Directory utenti e gruppi; raggiungibile solo sulla rete interna del compose | FR-03, FR-06 |
| janus-agent | Telemetria, docker proxy, exec/PTY, buffer, update | FR-20..23, FR-83, FR-84 |

`agent-gateway` nell'MVP può essere un modulo dello stesso processo `api`; resta separabile
perché comunica solo via Redis/PG (scalabilità oltre A2).

### 3.2 Struttura del repository

Stato attuale (v0.4). Le cartelle marcate *(F…)* arrivano con la fase indicata.

```
janus-ops/
├── backend/
│   ├── pyproject.toml, uv.lock     dipendenze pinnate (uv)
│   ├── src/janus_ops/
│   │   ├── cli.py                  janus-ops serve | migrate | version
│   │   ├── config.py               Settings da variabili JANUS_*
│   │   ├── db.py, health.py        engine async, /health, /ready
│   │   ├── main.py                 app factory, header di sicurezza, UI statica
│   │   ├── logsetup.py             log JSON su stdout
│   │   ├── migrations/             Alembic (SQL scritto a mano, niente autogenerate)
│   │   └── modules/  (F0+)         auth, inventory, vault, audit, ssh, monitoring, ...
│   └── tests/                      unit + integration (TimescaleDB reale)
├── frontend/
│   ├── app/, components/           App Router, Sidebar, StatusBadge
│   └── scripts/sync-assets.mjs     copia icone e logo da res/ in public/
├── agent/  (F2)                    janus-agent in Go
├── proto/  (F2)                    contratti agent ⇄ server
├── deploy/                         compose.yaml, compose.build.yaml, Caddyfile, .env.example
├── scripts/                        gen-env.sh, integration-test.sh, release.sh
├── docs/                           specs, PLAN, dockerhub/
├── res/                            brand & design resources
├── Dockerfile, .dockerignore       immagine unica (ADR-009)
├── .github/workflows/ci.yml        lint, test, integration, build; push su tag v*
└── Makefile
```

### 3.3 ADR

**ADR-001 — Monorepo**
- *Opzioni*: (a) monorepo unico — versionamento atomico dei contratti agent⇄server, CI unica; (b) repo separati — release indipendenti, ma contratti duplicati e drift.
- *Decisione*: **(a)**. I contratti `proto/` devono cambiare insieme ad agent e gateway.
- *Conseguenze*: CI con job per linguaggio filtrati per path.

**ADR-002 — Trasporto agent: WSS + multiplexing**
- *Opzioni*: (a) gRPC bidi su HTTP/2 — tipizzato, ma fragile con proxy TLS-inspecting e alcuni L7 LB; (b) WebSocket su HTTP/1.1 Upgrade + multiplexer di stream (es. yamux `[VERIFY: libreria]`) + messaggi protobuf — massima compatibilità con proxy; (c) SSH reverse tunnel — maturo ma porta 22 spesso bloccata e gestione chiavi più complessa.
- *Decisione*: **(b)**. Un canale di controllo + stream dedicati per metriche, docker, exec, PTY.
- *Conseguenze*: si implementano a mano heartbeat, backpressure e reconnect (backoff esponenziale con jitter). Autenticazione mTLS (cert per-agent da enrollment, vedi C7). Se un proxy TLS-inspecting rompe l'mTLS: fallback a token firmato a breve scadenza dentro il tunnel `OPEN`.

**ADR-003 — Task queue e workflow**
- *Opzioni*: (a) Celery + Canvas — maturo, ma sync-first e Canvas poco adatto a workflow con attese umane; (b) Taskiq (async) + workflow engine interno (DAG persistito in PG, step idempotenti) — coerente con FastAPI async, stato ispezionabile in SQL; (c) Temporal — durabilità eccellente, ma un servizio pesante in più da operare.
- *Decisione*: **(b)** `[VERIFY: maturità Taskiq al bootstrap; alternativa equivalente: arq]`.
- *Conseguenze*: l'engine workflow (F8) va progettato con stati espliciti (pending/running/waiting_approval/failed/done) e retry idempotenti.

**ADR-004 — Secret vault**
- *Opzioni*: (a) cifratura applicativa AES-256-GCM con envelope encryption (DEK per segreto, KEK da master key in env/file) — nessuna dipendenza; (b) OpenBao/Vault esterno — più robusto ma un servizio in più; (c) pgcrypto — chiave visibile al DB.
- *Decisione*: **(a)** dietro un'interfaccia `SecretBackend`, così (b) diventa un'implementazione aggiuntiva (FR-86).
- *Conseguenze*: procedura di rotazione KEK documentata; la master key non va mai nel DB né nei backup DB.

**ADR-005 — Storage metriche**
- *Opzioni*: (a) TimescaleDB nello stesso Postgres — un solo DB, SQL e JOIN con inventory; (b) Prometheus/VictoriaMetrics — ottimi per metriche, ma secondo sistema e modello dati separato.
- *Decisione*: **(a)** con hypertable, compressione dopo 7 giorni, continuous aggregates 1m/1h, retention raw 30 giorni `OPEN` (vedi C12).
- *Conseguenze*: ingest in batch (COPY / insert multi-riga) dal gateway; schema metriche "narrow" (`time, node_id, metric_id, value`) + tabella `metric_series` per le label.

**ADR-006 — Frontend e sessione**
- *Opzioni*: (a) Next.js come SPA prevalentemente client-side, API FastAPI stessa origine via reverse proxy, sessione in cookie httpOnly; (b) Next.js come BFF con proxy delle API — più codice, doppia auth.
- *Decisione*: **(a)**. Niente segreti né logica di autorizzazione nel layer Next.
- *Conseguenze*: CSRF token per richieste mutanti; WebSocket autenticati con lo stesso cookie + verifica Origin.
- **AMEND Architect (v0.4)**: il frontend è compilato con `output: 'export'` (file statici) e servito dal processo FastAPI, non da un server Next.js — per permettere l'immagine unica (ADR-009). Conseguenze: niente SSR, Server Actions né rotte dinamiche senza `generateStaticParams`; le pagine di dettaglio useranno parametri in query string o routing client-side. Nessun requisito ne dipende, perché con (a) tutto il rendering era già lato client.

**ADR-009 — Packaging: immagine unica**
- *Contesto*: un solo repository Docker Hub (`lordraw77/janus-ops`) e un deploy semplice per ambienti air-gapped.
- *Opzioni*: (a) **immagine unica**: API + UI esportata, stessa immagine anche per `migrate` e per i futuri worker (comando diverso) — un artefatto, una versione; (b) immagini separate `-api` e `-frontend` — scalano separatamente, ma due repository e un container Node in più senza benefici, dato che l'UI non fa SSR.
- *Decisione*: **(a)**. Build multi-stage: Node 24 per l'export della UI → uv per il virtualenv → runtime `python:3.12-slim`, utente non-root `10001`.
- *Conseguenze*: TLS fuori dall'immagine (Caddy nella stack di riferimento); i ruoli futuri (worker, scheduler, agent-gateway) saranno sottocomandi di `janus-ops` sulla stessa immagine; i binari dell'agent (F2) potranno essere inclusi nell'immagine per l'auto-update air-gapped.

**ADR-010 — Ruoli e permessi configurabili (v0.6)**
- *Contesto*: l'utente chiede che l'accesso all'audit log (e in generale ai moduli) sia configurabile dalla gestione utenti.
- *Opzioni*: (a) ruoli fissi in codice (stato v0.5) — semplice ma ogni cambio richiede un rilascio; (b) **permessi definiti nel codice, ruoli nel database** — ogni ruolo = gruppo LDAP + insieme di permessi, modificabili dalla UI; (c) policy engine (es. OPA/Casbin) — potente ma sproporzionato per l'MVP.
- *Decisione*: **(b)**. Catalogo permessi in `rbac.py` (`audit.read`, `users.read`, `users.manage`, `roles.manage`; cresce con i moduli). Ruoli built-in `admin` (tutti i permessi, non modificabile), `operator`, `auditor`; ruoli custom creabili. Più ruoli = unione dei permessi.
- *Conseguenze*: la sessione salva i ruoli al login; i permessi sono ricalcolati a ogni richiesta, quindi una modifica vale subito anche per chi è collegato. Anti-escalation: solo un admin può assegnare/togliere il ruolo admin o agire su un admin; nessuno può eliminarsi, disabilitarsi o togliersi admin. Chi ha `roles.manage` può però darsi altri permessi: per questo di default ce l'ha solo admin. `JANUS_LDAP_GROUP_*` rimossi (AMEND ADR-008).

**ADR-007 — Integrazione AI**
- *Decisione*: client OpenAI-compatible verso endpoint **self-hosted esterni** (A6), con tool **read-only** esposti dal backend (get_logs, get_metrics, get_audit, get_inventory). LiteLLM solo se serve il routing tra più endpoint/modelli; altrimenti SDK diretto `[VERIFY in F7]`. Le azioni sono prodotte come *Action Plan* strutturato e passano dal normale flusso RBAC + approvazione.
- *Conseguenze*: nessuna redazione dei dati operativi (consentiti da A6); i segreti del vault non sono mai raggiungibili dai tool; ogni prompt/risposta registrato nell'audit; più endpoint configurabili (es. modello piccolo per triage, grande per RCA).

**ADR-008 — Directory utenti: LDAP sidecar**
- *Contesto*: A5 richiede LDAP come container sidecar nello stack, in modo che utenti e gruppi siano gestiti fuori dal DB applicativo.
- *Opzioni*: (a) **lldap** — server LDAP leggero con interfaccia web per utenti e gruppi; un solo container, poca configurazione, ma implementa solo un sottoinsieme di LDAP `[VERIFY: attributi di gruppo/memberOf, supporto LDAPS]`; (b) **OpenLDAP** (immagine Bitnami o simile `[VERIFY: immagine mantenuta]`) — LDAP completo, ma gestione schemi e ACL onerosa e nessuna interfaccia inclusa; (c) **389-DS / FreeIPA** — directory enterprise complete, troppo pesanti per un sidecar.
- *Decisione*: **(a) lldap**, dietro un'interfaccia `DirectoryBackend` basata su LDAP standard (bind + search). Così passare a OpenLDAP o a un AD esterno (FR-07) richiede solo configurazione.
- *Dettagli (implementati, v0.5)*:
  - **Flusso**: bind con l'account di servizio `janus-svc` (gruppo `lldap_strict_readonly`) → ricerca dell'utente con filtro configurabile e valore escapato → esattamente un risultato, altrimenti credenziali non valide → bind con il DN dell'utente e la password inserita → gruppi da `memberOf`.
  - **Ruoli**: dal CN dei gruppi (`JANUS_LDAP_GROUP_*`, default `janus-admins` / `janus-operators` / `janus-auditors`); vince il ruolo più alto; nessun gruppo = accesso negato (403 `no_role`). Il ruolo è fotografato nella sessione al login.
  - **Client**: **ldap3** 2.9.1 (Python puro) in thread pool. Alternative valutate: *bonsai* (async e mantenuto, ma senza wheel Linux → compilazione con libldap nell'immagine) e *python-ldap* (estensione C, stesso problema). **Rischio residuo**: ultima release di ldap3 nel 2021 e uso di API pyasn1 deprecate (warning filtrati nei test). Mitigazioni: versioni bloccate in `uv.lock`, test di integrazione su lldap reale in CI, interfaccia `DirectoryBackend` per sostituirlo.
  - **Provisioning**: il job `ldap-init` usa `/app/bootstrap.sh` dell'immagine lldap (idempotente) per creare gruppi e account di servizio. Il `curl` dell'immagine non usa il DNS di Docker: il nome del servizio viene risolto con `getent`.
  - **Sessioni**: token casuale da 256 bit in cookie `HttpOnly; Secure; SameSite=Strict`, solo lo SHA-256 nel DB; durata 8 h, inattività 1 h; logout e rotazione break-glass revocano le sessioni.
  - **Protezioni**: max 5 errori per username e 30 per IP in 15 minuti (in memoria, processo singolo come da A2); scritture `/api` con Origin/Referer diverso dall'host rifiutate (CSRF); password vuote rifiutate prima del bind; username limitati a `[A-Za-z0-9._@-]{1,64}`.
  - **Gestione utenti (v0.6)**: account `janus-manager` nel gruppo `lldap_admin`; utenti e gruppi via API GraphQL di lldap (HTTP con la libreria standard), password via LDAP Password Modify (RFC 3062), perché lldap salva le password con OPAQUE e GraphQL non può impostarle. Con una directory esterna (`JANUS_LLDAP_HTTP_URL` non impostato) la gestione utenti è in sola lettura, la disabilitazione lato Janus resta possibile.
  - **TOTP (v0.6)**: RFC 6238 implementato con la libreria standard (verificato con i vettori ufficiali), QR generato in locale (`segno`), segreti cifrati AES-256-GCM (`cryptography`) con `JANUS_SECRET_KEY` e legati all'utente; finestra ±1 passo, anti-replay, 10 codici di recupero monouso, 5 errori chiudono la sessione in attesa; dopo il secondo fattore viene emesso un token nuovo. Nessuna chiamata di rete: funziona dietro proxy aziendali e air-gapped.
  - **Break-glass**: un solo account locale (`JANUS_BREAKGLASS_USERNAME`), hash scrypt (stdlib), impostato con `janus-ops breakglass`; il suo nome non passa mai da LDAP.
- *Conseguenze*: LDAP diventa una dipendenza del login, mitigata dall'account break-glass (FR-06). Il container LDAP non espone porte all'esterno del compose; la sua interfaccia web va pubblicata solo dietro il reverse proxy e solo per gli Admin `OPEN`. Il volume dati LDAP entra nella procedura di backup.

### 3.4 Flussi principali

**Bootstrap agent (F2)**
1. Operator abilita "Janus Agent" su un nodo con capability SSH.
2. Worker: SSH → `uname -m`, detect init system → SFTP del binario in `/usr/local/bin/janus-agent`.
3. Server genera **token di enrollment monouso** (TTL breve) + scrive `/etc/janus-agent/config.yaml` (URL, CA, token, proxy, policy).
4. Unit systemd avviata → agent si connette, presenta il token, riceve un certificato client (CSR generata sull'agent: la chiave privata non lascia mai il nodo).
5. Il token viene invalidato; il nodo passa "online".

**Telemetria → stato → notifica (F2–F3)**
agent (batch ogni N s, buffer bbolt) → gateway (validazione, insert batch) → Redis pub/sub → evaluator nel worker (soglie, isteresi, dipendenze, downtime) → `problem` aperto/chiuso → notification router → WS al browser.

---

## 4. Data Engineer — Modello dati (logico)

| Entità | Campi chiave | Note |
|---|---|---|
| `user` | id, ldap_dn, username, totp_secret_enc, totp_enabled, totp_last_step, is_breakglass, pwd hash (scrypt, solo break-glass), is_active, last_login | Profilo locale creato al primo login LDAP |
| `role`, `role_permission` | name, description, ldap_group (unico), builtin; (role, permission) | **AMEND Data Engineer (v0.6)**: sostituiscono `role_mapping` (migrazione `0002`) |
| `user_session` | token_hash, roles[], stage (`mfa_pending`/`full`), mfa_failures, scadenze | `0002`: più ruoli per sessione e passo TOTP |
| `totp_recovery_code` | user_id, code_hash (SHA-256), used_at | Codici monouso |
| `node` | id, name, fqdn/ip, environment, parent_id, created_by | `parent_id` → dipendenze (FR-32) |
| `node_capability` | node_id, type (`agent/ssh/proxmox/docker/snmp`), config jsonb, credential_id | |
| `tag`, `node_tag`, `group`, `node_group` | | Inventory Ansible dinamico |
| `credential` | id, type, ciphertext, dek_wrapped, kek_version | Mai restituito in chiaro via API |
| `agent` | node_id, cert_fingerprint, version, arch, last_seen, policy jsonb | |
| `enrollment_token` | hash, node_id, expires_at, used_at | Solo l'hash in DB |
| `metric_series` | id, node_id, name, labels jsonb | Indice unico (node_id, name, labels) |
| `metric_point` (hypertable) | time, series_id, value | Indice (series_id, time DESC) |
| `check` | id, node_id, type, params, warn/crit, cycles, interval | |
| `check_state` | check_id, state, since, consecutive, last_output | Stato corrente |
| `check_result` (hypertable) | time, check_id, state, output | Storico |
| `problem` | id, check_id, opened_at, closed_at, ack_by, ack_comment | Tabella "unhandled problems" |
| `downtime` | scope (node/check/group), start, end, author, reason | |
| `notification_channel`, `notification_rule` | | Escalation |
| `audit_event` | id, ts, actor, action, target, payload, prev_hash, hash | Append-only, hash chain |
| `terminal_session` | id, user, node, started, ended, recording_path | |
| `playbook`, `job_run`, `job_event` | | ansible-runner events |
| `workflow`, `workflow_run`, `workflow_step_run` | | F8 |
| `ai_conversation`, `action_plan` | status (draft/approved/rejected/executed) | |

Consistenza: transazionale (PG) per inventory/vault/audit; eventual (≤ ciclo check) per gli stati di monitoring.
Migrazioni: Alembic, up/down, reversibili. Lo **schema DDL F0–F3** è nella migrazione
[`0001_initial_schema.py`](../backend/src/janus_ops/migrations/versions/0001_initial_schema.py):
ogni indice ha un commento con il pattern di query che lo giustifica. Scelte rilevanti: enumerazioni come
`text` + `CHECK` (niente `ALTER TYPE`), hypertable `metric_point` (compressione 7 g, retention 30 g) e
`check_result` (compressione 7 g, retention 90 g) senza FK verso le tabelle padre, `audit_event`
append-only tramite trigger (UPDATE/DELETE/TRUNCATE bloccati), al massimo un problema aperto per check
e un solo account break-glass (indici unici parziali). Continuous aggregates rimandati a F2, quando
esisteranno le query delle dashboard. Le tabelle di F4+ arriveranno con migrazioni dedicate.

---

## 5. Roadmap per fasi

Ogni fase termina con un rilascio installabile e i propri criteri di accettazione verdi.

| Fase | Contenuto | Uscita |
|---|---|---|
| **F0 Foundations** | Monorepo, Makefile, CI (lint/test per linguaggio), compose (proxy, app, migrate, PG+TS, LDAP sidecar), login LDAP + TOTP + break-glass, RBAC da gruppi LDAP, inventory CRUD, vault, audit con hash chain, UI shell (sidebar, command palette, design tokens Obsidian Graphite) | AC-01, AC-02, AC-02b, AC-02c |
| **F1 SSH Bastion** | AsyncSSH ⇄ WS ⇄ xterm.js, credenziali dal vault, multi-tab, SFTP, ProxyJump, recording | AC-03 |
| **F2 Agent & Telemetry** | Agent Go (tunnel, telemetria, buffer), gateway, enrollment mTLS, bootstrap SSH, ingest Timescale, NOC dashboard (hexagon, node cards) | AC-04, AC-05 |
| **F3 Alerting** | Check engine, isteresi, problems, ack, downtime, dipendenze, flapping, notifiche, discovery | AC-06..08 |
| **F4 Containers** | Docker proxy nell'agent, viste container, log/stats/exec, compose stack | |
| **F5 Proxmox** | Client PVE, overview cluster, lifecycle VM/LXC, backup audit, console | |
| **F6 Ansible** | Inventory dinamico, catalogo da Git, survey, run live, storico | |
| **F7 AI Copilot** | Diagnose su problem, tool read-only, action plan + approvazione | |
| **F8+** | Workflow visuale, GitOps, SNMP, PTY/exec via agent, auto-update firmato, NTLM, AD/LDAP esterno, OIDC, agent Windows | |

Razionale dell'ordine: F1 dà valore immediato senza agent e costruisce i mattoni (vault, WS, audit) riusati

### 5.1 Avanzamento F0

| Elemento | Stato | Verifica |
|---|---|---|
| Monorepo, Makefile, `.env.example`, script | ✅ | `make help`; `make env` idempotente |
| Schema DDL F0–F3 (migrazione `0001`) | ✅ | 13 test di integrazione su TimescaleDB 2.32 reale: round trip up/down/up, hypertable e policy, trigger append-only, vincoli |
| Backend: config, log JSON, `/health`, `/ready`, CLI | ✅ | 21 unit test, ruff, mypy strict |
| UI shell (sidebar dal mockup, token, favicon, stato del control plane) | ✅ | ESLint, `tsc`, export statico |
| Immagine unica + Compose (Caddy, app, migrate, TimescaleDB, lldap) | ✅ | Stack avviata: HTTPS, `/ready` = ok, migrazione ripetuta senza effetti |
| CI GitHub Actions + publish Docker Hub su tag | ✅ | actionlint, shellcheck; prima esecuzione al push |
| Login LDAP (username + password), ruoli da gruppi, sessioni, `require_role` | ✅ | 26 test di integrazione su lldap + TimescaleDB reali, inclusi AC-02b (cambio gruppo via API lldap) e AC-02c (LDAP spento) |
| Break-glass (`janus-ops breakglass`) | ✅ | Test di integrazione + prova nello stack Compose |
| Audit con hash chain (scrittura e verifica) | ✅ | `verify_chain` nei test; trigger append-only |
| Pagina di login, guardia sulle pagine, utente e logout nella sidebar | ✅ | ESLint, `tsc`, export statico; prova HTTPS via Caddy |
| Amministratore iniziale da `.env` (`janus-ops setup`, solo se assente) | ✅ | Test di integrazione + stack Compose (rilancio idempotente) |
| Gestione utenti (API + pagina Users & roles) | ✅ | Test: ciclo di vita completo, validazioni, anti-escalation, anti-lockout |
| Ruoli e permessi configurabili, ruoli custom (ADR-010) | ✅ | Test: permesso `audit.read` concesso/revocato con effetto immediato su sessioni attive |
| TOTP opzionale (setup con QR, login in due passi, recovery, reset admin) | ✅ | Vettori RFC 6238; test di integrazione; prova HTTPS via Caddy |
| Consultazione audit (API + UI, filtri, verifica catena) | ✅ | AC-03 |
| Inventory CRUD, vault | ⏳ | AC-01 |
| Command palette `Ctrl+K` | ⏳ | FR-05 |

Rimandati alla fase che li usa, per la regola "nessun componente senza requisito": **Redis** e
**worker/scheduler** (F2–F3), **agent-gateway** (F2).
da tutto il resto; F2–F3 sono il cuore "Checkmk"; Docker prima di Proxmox perché riusa il tunnel appena costruito.

---

## 6. Security — Threat model iniziale (STRIDE)

Trust boundary: Browser ⇄ Server · Agent ⇄ Server · Server ⇄ target (SSH/PVE/SNMP) · Server ⇄ LLM · Server ⇄ Git.

| Minaccia | Vettore | Mitigazione | Rischio residuo |
|---|---|---|---|
| S — Agent impersonato | Furto token di enrollment | Token monouso a TTL breve, solo hash in DB, mTLS dopo enrollment, revoca cert | Basso |
| S — Sessione utente rubata | XSS, furto cookie | Cookie httpOnly/Secure/SameSite, CSP rigorosa, TOTP | Basso |
| T — Binario agent alterato | Server o canale compromesso durante update | Firma ed25519 con chiave offline, verifica sull'agent | Medio (se chiave di release compromessa) |
| T — Manomissione audit | Admin DB | Append-only via grant, hash chain, export esterno | Medio |
| R — Azioni non attribuibili | Account condivisi, azioni AI | Audit per ogni azione con attore; azioni AI attribuite all'approvatore | Basso |
| I — Leak segreti | API, log, prompt LLM | Segreti mai serializzati verso client; log strutturati con redazione; tool AI senza accesso al vault | Basso |
| I — Dati operativi verso host LLM | Log/metriche inviati a host esterni allo stack | Solo modelli self-hosted (A6), TLS verso gli endpoint, endpoint configurabili solo da Admin e auditati | Basso (dati accettati da A6) |
| S — Login LDAP aggirato | LDAP injection nei filtri, bind anonimo o con password vuota | Escaping dei filtri, rifiuto delle password vuote, bind anonimo disabilitato | Basso |
| I — Credenziali LDAP esposte | Traffico in chiaro, password dell'account di servizio | LDAP raggiungibile solo sulla rete interna del compose (LDAPS se il traffico esce dall'host), account di servizio read-only da secret | Basso |
| D — LDAP non disponibile | Container fermo o corrotto | Account break-glass, healthcheck, backup del volume LDAP | Basso |
| E — Escalation via gestione utenti | Chi ha `users.manage` si assegna admin | Solo admin assegna/toglie admin o modifica un admin; test dedicati | Basso |
| E — Account `janus-manager` compromesso | È amministratore della directory | Password solo in `.env` (600) e nel container app; mai esposta; rete `backend` interna | Medio (intrinseco) |
| I — Perdita di `JANUS_SECRET_KEY` | Backup del DB senza la chiave | Seed TOTP illeggibili → reset 2FA da admin; chiave da salvare insieme al DB (README) | Basso |
| D — Flood dagli agent | Agent compromesso o buggato | Rate limit per agent, dimensione massima batch, backpressure | Basso |
| E — RCE di massa dal server | Server compromesso → exec su tutta la flotta | Policy locale sull'agent non sovrascrivibile da remoto, RBAC, approvazione per broadcast | **Alto: è intrinseco al prodotto** — documentarlo |
| E — Prompt injection via log | Log contenenti istruzioni per l'AI | Tool read-only, nessuna esecuzione senza umano, action plan mostrato per intero | Medio |
| E — Escalation RBAC | Endpoint senza check | Deny-by-default, dipendenza FastAPI obbligatoria per ruolo, test per endpoint | Basso |

Requisiti di sicurezza per l'Engineer: versioni dipendenze pinnate (lockfile), SBOM in CI, container non-root,
nessun segreto in immagini/log, validazione input con Pydantic su ogni boundary.

---

## 7. Rischi di progetto

| Rischio | Probabilità | Impatto | Mitigazione |
|---|---|---|---|
| Scope creep (8 moduli) | Alta | Alto | Fasi rilasciabili, WON'T espliciti |
| Complessità tunnel agent (multiplexing, proxy) | Media | Alto | Spike tecnico all'inizio di F2 con proxy reale |
| ldap3 non più rilasciato (ultima release 2021) | Media | Medio | Versioni bloccate, test su lldap reale in CI, sostituibile dietro `DirectoryBackend` (ADR-008) |
| Performance ingest Timescale | Bassa (con A2) | Medio | Batch insert, test di carico in F2 |
| Compatibilità API Proxmox tra versioni | Media | Medio | Test contro PVE 8.x `[VERIFY: versioni target]` |
| Qualità diagnosi AI con LLM locali | Media | Medio | Prompt e tool deterministici, metriche di valutazione |

---

## 8. Risorse grafiche (`res/`)

`res/` è l'**unica cartella degli asset di design** (v0.3). Dal `janusops-design-kit/` è stato preso il
mockup; tutto il resto del kit era già identico a `res/` oppure mancava. L'inventario completo è in
[res/README.md](../res/README.md).

### 8.1 Contenuto

| Percorso | Contenuto | Origine |
|---|---|---|
| `Janus-Ops-logo.png` | Logo master 2048×2048, sfondo trasparente | fornito |
| `logos/janus-ops-logo-{256,512}.png` | Logo ritagliato sul contenuto, trasparente | generato dal master |
| `logos/janus-ops-app-icon-512.png` | Icona arrotondata su `#090A0F` con bordo `#10B981` | generato |
| `favicons/` | `favicon.ico` (16–64), PNG 16/32, `apple-touch-icon.png` 180, `android-chrome` 192/512, `site.webmanifest` | generato |
| `design-tokens.json`, `globals.css` | Token e CSS base Tailwind v4; aggiunto `text.nav` / `--text-nav` = `#94A3B8` | fornito + v0.3 |
| `obsidian-graphite-palette-sheet.png` | Palette visuale | fornito |
| `HexagonHealthWidget.tsx`, `NodeEnvironmentCard.tsx` | Componenti di riferimento | forniti |
| `mockups/janus-ops-noc-preview.html` | Anteprima dell'app shell, brand corretto in "Janus-Ops" | dal design kit |

Limiti noti:
- Il logo è molto dettagliato: a 16×16 è poco leggibile. Un marchio semplificato per le dimensioni piccole è un miglioramento possibile, non bloccante.
- Il render `janusops-dashboard-final.jpg` citato dal kit non esiste. Nel mockup è sostituito da un segnaposto e il riferimento per il layout NOC resta la §9 di [specs.md](specs.md) più la §8.2 qui sotto.
- `janusops-design-kit/` è un duplicato di `res/` e non è incluso nel repository.

### 8.2 Elementi ricavati dal mockup NOC

- **App shell**: sidebar fissa a sinistra (`w-56`, `--bg-surface`, bordo destro `--border-default`) + area principale con padding 24 px.
- **Brand in sidebar**: logo 40 px (`logos/janus-ops-logo-256.png`) + wordmark "Janus-" in `--text-primary` e "Ops" in `--accent-primary`, sottotitolo mono 10 px "NOC CONTROL PLANE" in `--text-muted`.
- **Ordine di navigazione**: Dashboard · Containers · Proxmox VE · Ansible & Workflows · SSH Bastion · Janus AI Copilot. Coincide con i moduli; mancano Inventory, Alerts/Problems, Audit, Settings, da aggiungere.
- **Voce attiva**: sfondo `--bg-elevated`, testo `--accent-primary`, bordo `--border-default`. **Voce inattiva**: `--text-nav` (`#94A3B8`, aggiunto ai token in v0.3). La voce AI usa sempre `--accent-ai`.
- **Footer della sidebar**: versione del design system in mono ("Obsidian Graphite v1.0").
- Il mockup carica Tailwind da CDN: va bene per un'anteprima, non per l'app, che deve funzionare air-gapped (asset e font tutti locali).

### 8.3 Note per il design system (F0)

- I token JSON contengono anche `semantic.*.subtleBg` e `brand.primaryHover`, assenti nel CSS: unificare generando il CSS dai token.
- I componenti di esempio usano colori hex inline (`bg-[#12141D]`): in F0 vanno convertiti in classi basate sulle variabili/tema Tailwind v4.
- `HexagonHealthWidget` riempie l'esagono interno sempre di rosso (`rgba(239,68,68,0.18)`) anche se lo stato è OK: il fill va derivato dallo stato come lo stroke.
- Font: Inter (UI) + JetBrains Mono (metriche, tabular-nums), da distribuire self-hosted (ambienti air-gapped).
- Favicon e icone PWA in `res/favicons/` vanno copiate in `frontend/public/` (o `app/`) in F0 e collegate nei metadata di Next.js `[VERIFY: convenzioni file-based icons della versione scelta]`.

---

## 9. Domande

### 9.1 Risolte

| # | Domanda | Risposta | Effetto |
|---|---|---|---|
| 1 | Scala target | ≤ 500 nodi, ≤ 20 utenti, "per il momento" | A2 confermata |
| 2 | Autenticazione | LDAP come sidecar Docker, directory nello stack | A5 rivista, ADR-008, FR-03/06/07 |
| 3 | Agent Windows nell'MVP | No | A4 confermata, Windows in F8+ |
| 4 | Dati verso l'AI | Tutti i dati operativi; modelli locali su host esterni | A6 rivista, ADR-007, threat model |
| 5 | Licenza TimescaleDB | Apache 2 (core) + TSL (Community) | C12 verificato |
| 6 | Licenza Janus-Ops | Apache-2.0 | A8, `LICENSE`, `NOTICE` |
| 7 | Grafia del brand | Janus-Ops | C15 |
| 8 | Asset del logo | Master `Janus-Ops-logo.png`; favicon da generare | `res/logos/`, `res/favicons/` |
| 9 | Cartella asset | `res/`, prendendo dal design kit quello che serve | §8 |

### 9.2 Aperte

Nessuna domanda bloccante. Restano i punti `[VERIFY]` da chiudere durante le singole fasi.

---

```
STATE
  Tier:        L
  Phase:       Engineer — F0 in corso (identità e accessi completati)
  Delivered:   piano v0.6 (ADR-001..010), schema F0–F3 + 0002, login LDAP, admin iniziale da .env,
               gestione utenti/ruoli/permessi, TOTP opzionale, viewer audit, UI, compose, CI
  Open:        CSP per l'UI esportata; [VERIFY] Proxmoxer async, aiodocker ssh, NTLM Go, yamux, Taskiq
  Next:        inventory (nodi, tag, gruppi, capability) e vault credenziali
```
