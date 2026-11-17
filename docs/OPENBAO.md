# OpenBao nella stack: la chiave master fuori da Janus-Ops (FR-86)

`deploy/compose.openbao.yaml` aggiunge un OpenBao (storage raft, TLS interno) che si **sblocca da solo**
a ogni riavvio con una chiave statica (`seal "static"`). Janus-Ops lo usa come motore Transit (la chiave
master non esce mai da OpenBao) o come KV v2.

## Prima volta

    scripts/openbao-setup.sh                      # chiavi, certificati, init, Transit, KV, AppRole, .env
    make docker/run COMPOSE_EXTRA="-f deploy/compose.openbao.yaml"   # (con il tuo elenco di override)
    docker compose ... exec janus-ops janus-ops secrets migrate --to openbao-transit
    # poi in deploy/.env: JANUS_SECRET_BACKEND=openbao-transit, e ricrea janus-ops

Lo script è idempotente e non stampa segreti. Scrive in `deploy/secrets/openbao/` (ignorata da git):
`seal.key` (chiave di unseal), `tls/` (CA e certificato), `init.json` (chiave di recupero; il root token
viene revocato a fine setup) e in `deploy/.env` i valori `JANUS_BAO_*` (role id e secret id dell'AppRole).

## Cosa protegge e cosa no

- La chiave di unseal sta **sullo stesso host** dei dati: protegge da una copia del solo volume dati o
  da un backup, non da chi controlla l'host. È il prezzo dello sblocco automatico (OpenBao lo sconsiglia
  se non c'è già un'altra radice di fiducia). Alternativa più forte: unseal manuale con chiavi Shamir
  custodite da persone, o un KMS esterno, con il costo di un intervento a ogni riavvio.
- **Fai il backup** di `deploy/secrets/openbao/` (soprattutto `seal.key` e `init.json`) **separatamente**
  dal volume `openbao_data` e dal dump del DB: senza la chiave di unseal i dati di OpenBao, e quindi tutti i
  segreti in Transit, sono persi. Snapshot dei dati: `bao operator raft snapshot save`.
- Rotazione della chiave Transit: `bao write -f transit/keys/janus-kek/rotate`; i vecchi segreti restano
  leggibili. Nuovo secret id dell'app: `scripts/openbao-setup.sh --new-secret-id` e ricrea janus-ops.

## Se OpenBao non c'è

Credenziali, canali di notifica e TOTP non si leggono: le funzioni che li usano rispondono
`vault_unavailable` e `/api/v1/ready` mostra `secret_manager: unreachable`. Il login con password senza
TOTP e l'account break-glass continuano a funzionare. Tornare indietro:
`janus-ops secrets migrate --to local` (serve `JANUS_SECRET_KEY` ancora impostata).
