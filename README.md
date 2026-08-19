# VirtizAI

VirtizAI is a self-hosted AI orchestration platform for conversations, tools, projects, and model-provider workflows under your control.

## Features

- WebUI, Discord, and CLI interfaces backed by one API and session model
- Configurable routing across local and cloud model providers
- Provider discovery, health checks, fallback routing, and model metadata
- Persistent context, memory, projects, environments, and conversation history
- Policy-controlled tools and execution with an audit trail
- SQLite/WAL durable state with versioned migrations
- Verified updates, known-good promotion, rollback, and failure recovery
- Docker Compose and native Debian/Ubuntu packaging

Local providers can handle private workloads while cloud providers remain available when explicitly configured. Secrets are referenced through the secret-store boundary rather than stored as ordinary product data.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
uvicorn virtizai_core.main:app --app-dir .
```

Use `make compose-up` for Docker Compose or `make package` to build a native package. See [DEPLOYMENT.md](DEPLOYMENT.md) for installation and storage guidance.

## Development

```bash
make test
make build
make package
make docker
```

Architecture and contribution guidance is in [docs/development.md](docs/development.md). Release procedures are in [RELEASES.md](RELEASES.md).

## Project status

VirtizAI is an early, actively developed 0.1 release. The application, interfaces, provider routing, persistence, execution controls, packaging, and update/recovery workflows are implemented and covered by automated tests. APIs and deployment configuration may evolve before a stable 1.0 release.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changes. Report security issues privately using [SECURITY.md](SECURITY.md). A project license remains an owner decision and is not declared here.
