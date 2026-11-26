# AI providers, models and fallback routes

Ported from the AIFRED gateway (`/opt/AIFRED/aifred/providers.py`, `router.py`).

- **Provider**: an OpenAI-compatible endpoint. Presets: Groq, OpenRouter, Google Gemini (billed, created
  switched off), Cloudflare Workers AI (needs the account id), Cerebras, Mistral, NVIDIA, Hugging Face, Ollama,
  local OpenAI-compatible, custom. Timeouts, extra headers/body, TLS verification and token budgets
  (daily, monthly) per provider.
- **Keys**: any number per provider, each sealed by the vault (shown as the last four characters). A key
  rejected by the provider is switched off; one that hit its quota rests until it recovers; the next key of the
  same provider is tried first.
- **Models**: read from the provider (`Refresh models`) or added by name. `Test` sends one request with a tool:
  a model that answers can go on a route; the badge says whether it also calls tools.
- **Routes**: an ordered list of (provider, model). `default` serves the Copilot, Operate and the code
  generator, `review` the second opinion; others are custom (pick them in Operate). Failover rules:
  rate limit with a short `Retry-After` → wait and retry once; quota / bad key → next key, then next entry;
  context too long → next (larger) entry; model missing → marked unavailable, next entry; outage, timeout →
  the entry rests (30 s doubling to 10 min, 2 min for a timeout); a request the provider rejects → next entry
  without punishment (raised as is if every entry rejects it). Health survives restarts (`ai_health`) and is
  shared by every route using the same model. Every attempt is recorded (`ai_usage`).
- **Adding and choosing** (web UI): *Add provider* is a gallery of the presets with their address already filled in; a
  second provider of the same type gets the next free name (`groq`, `groq-2`), for two accounts. *Use this*, next to a
  provider's tested models, puts that model first on the `default` route and keeps the previous entries behind it as
  fallback. In *Settings → AI models & tools* two lists, *Main model* and *Second-opinion model*, do the same for the
  `default` and `review` routes. The old URL/model/key form there is now "Single endpoint (without providers)".
- **Compatibility**: until a route has a model, the single endpoint of Settings → AI models (and the
  `JANUS_AI_*` environment) answers exactly as before.
- **Import**: `janus-ops ai-import-providers --env-file FILE` (or `-` for stdin, or the process
  environment) creates providers from `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`,
  `CLOUDFLARE_API_KEY` + `CLOUDFLARE_ACCOUNT_ID`, `CEREBRAS_API_KEY`, `MISTRAL_API_KEY`, `NVIDIA_API_KEY`,
  `HF_TOKEN`, `OLLAMA_API_BASE`. It tries the connection first and prints only names and counts.
- **Permissions**: managing providers/routes needs `ai.manage`; picking a route or model in Operate needs
  `ai.operate`. Audit: `ai.provider.*`, `ai.route.*`.
