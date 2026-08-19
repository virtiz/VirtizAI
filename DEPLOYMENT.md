# VirtizAI Deployment

## Docker Compose

For a development checkout, use the version-aware command:

```bash
make compose-up
```

For a released source archive, set the release version explicitly before running
Compose. The OCI image tag, build argument, API, and WebUI then report the same
version:

```bash
VIRTIZAI_VERSION=0.1.1 docker compose up --build -d
```

Open `http://127.0.0.1:8766/` or the host's configured address. Persistent
volumes are `virtizai-data`, `virtizai-workspace`, and `virtizai-logs`. Ollama
is optional and is added through the WebUI as an Ollama provider; no Ollama,
Redis, Postgres, worker, or Agent01 service is required.

Stop/restart without losing state:

```bash
docker compose restart
docker compose down
```

Do not use `docker compose down -v` unless intentionally deleting VirtizAI
state. Portainer can deploy this same Compose definition as a Stack; use named
volumes or explicit bind mounts for `/data`, `/workspace`, and `/var/log/virtizai`.

## Native Debian/Ubuntu VM or LXC

Build or obtain the same `.deb` artifact, then install it on the clean Linux
host:

```bash
sudo apt install ./virtizai_0.1.0_amd64.deb
sudo systemctl status virtizai
```

The package creates the unprivileged `virtizai` service user and uses:

```text
/usr/lib/virtizai       immutable application
/etc/virtizai           bootstrap environment
/var/lib/virtizai       SQLite/WAL and durable state
/var/lib/virtizai/workspace  disposable jobs
/var/log/virtizai       logs
```

Use `journalctl -u virtizai` for logs. Normal package removal stops the service
but does not delete `/var/lib/virtizai`; reinstall preserves state.

Set optional bootstrap values in `/etc/virtizai/virtizai.env`:

```text
VIRTIZAI_HOST=127.0.0.1
VIRTIZAI_PORT=8766
```

## Same-host Ollama

Install Ollama independently on the same VM/LXC and add this provider in the
WebUI:

```text
http://127.0.0.1:11434
```

An optional trailing `/v1` is normalized for the native Ollama adapter. Models
are discovered from the provider; no model names are baked into VirtizAI.

## Remote Ollama

Add the reachable base URL of the remote Ollama endpoint, for example an
administrator-approved LAN address. Verify firewall/listen configuration on
the remote host; VirtizAI does not assume or modify that host.

## Backup boundary

Critical:

- `/var/lib/virtizai/virtizai.db`
- secret-store backend or secret references
- durable artifacts and configuration

Disposable:

- `/var/lib/virtizai/workspace`
- caches
- packaged static assets, which are restored by reinstall

Docker and native installations use the same SQLite/schema/data layout, so a
backup/export can conceptually move between them without rebuilding settings.

## Health

`GET /healthz` checks application/database health only. Optional model provider
failure does not make the application unhealthy.

## Build commands

```bash
make test
make build
make package
make docker
make checksums
```

The `.deb` builder supports amd64. arm64 is not claimed until validated.
