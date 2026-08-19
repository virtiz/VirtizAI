# Deploying VirtizAI

## Docker Compose

```bash
make compose-up
VIRTIZAI_VERSION=<version> docker compose up --build -d
```

Persistent volumes are `virtizai-data`, `virtizai-workspace`, and `virtizai-logs`. Portainer can deploy the same Compose definition as a Stack. Do not use `docker compose down -v` unless intentionally deleting application data.

## Native Debian or Ubuntu

```bash
sudo apt install ./virtizai_<version>_amd64.deb
sudo systemctl status virtizai
```

The package creates an unprivileged `virtizai` service user and uses `/usr/lib/virtizai` for the application, `/etc/virtizai` for bootstrap configuration, `/var/lib/virtizai` for SQLite/WAL and durable state, `/var/lib/virtizai/workspace` for disposable jobs, and `/var/log/virtizai` for logs.

Use `journalctl -u virtizai` for logs. Removing the package stops the service but preserves durable state.

## Providers and health

Add local or cloud providers through the WebUI/API. VirtizAI does not assume a provider URL or model name. `GET /healthz` checks application and database health; provider failure does not make the application unhealthy.

## Builds

```bash
make test
make build
make package
make docker
make checksums
make release-manifest
```

The native builder currently produces amd64 packages. Other architectures should not be advertised until validated.
