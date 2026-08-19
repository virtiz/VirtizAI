# Deploying VirtizAI

## Container images

Published release images are available from the canonical GitHub Container Registry (GHCR):

~~~bash
docker pull ghcr.io/virtiz/virtizai:<version>
VIRTIZAI_VERSION=<version> docker compose up -d
~~~

Use the image tag matching the GitHub Release. The release manifest and SHA-256 assets are the authoritative verification records.

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

## Release verification

GitHub Releases contain the versioned `.deb`, `release-manifest.json`, and `SHA256SUMS`. Verify the checksum before installation:

~~~bash
sha256sum -c SHA256SUMS
sudo apt install ./virtizai_<version>_amd64.deb
~~~

The manifest records the exact source commit, target schema, update/rollback compatibility, canonical GHCR image, and image digest. GitHub Actions provenance attestations are published for release artifacts and the container image.

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
