# Janus-Ops backend

FastAPI control plane. See the [project README](../README.md) and [docs/PLAN.md](../docs/PLAN.md).

```bash
uv sync                      # create .venv with dev dependencies
uv run pytest                # unit tests
uv run janus-ops --help      # CLI: serve | migrate | version
```

Configuration is read from `JANUS_*` environment variables; see [deploy/.env.example](../deploy/.env.example).
