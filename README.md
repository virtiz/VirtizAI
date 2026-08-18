# VirtizAI Core

Independent Phase 2 core for VirtizAI. This package is intentionally separate
from Hermes, Agent01, n8n, the existing Discord deployment, and all legacy state.

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
uvicorn virtizai_core.main:app --app-dir .
```

Set `VIRTIZAI_DATA_DIR`, `VIRTIZAI_WORKSPACE_DIR`, and `VIRTIZAI_LOG_DIR` for
persistent deployment paths. Defaults are local development directories.

The core creates a SQLite database in the data directory, enables WAL mode, and
applies versioned migrations on startup. Secret values are handled only through
the `SecretStore` abstraction; database rows contain references, never values.

This phase intentionally provides no WebUI and no live provider, Discord, SSH,
or infrastructure adapters.
