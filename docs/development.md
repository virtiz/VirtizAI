# Development guide

VirtizAI is a Python application with a FastAPI HTTP layer, SQLite/WAL persistence, provider adapters, routing, context and memory services, policy-controlled execution, and WebUI assets.

- `virtizai_core/` - application modules and internal architecture
- `tests/` - domain and integration tests
- `packaging/` - Debian packaging, systemd integration, and reusable validation tools
- `benchmarks/` - local performance measurements
- `webui/` - static WebUI

The `virtizai_core` module name is intentionally retained as an internal import boundary; the product and distribution name is VirtizAI. Validation tools accept explicit artifact and target parameters and must not embed private hosts, addresses, guest IDs, credentials, or production assumptions.

Database migrations are ordered and must be safe for existing SQLite state. Update and rollback changes preserve the unprivileged application / constrained-helper boundary, external transaction recovery, checksum validation, and known-good semantics.
