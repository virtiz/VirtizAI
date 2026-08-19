# VirtizAI Release Candidates

GitHub Releases are the canonical release source. Until the Phase 13 release gate
passes, every published VirtizAI release is a prerelease candidate. Immutable tags
and their assets are never rewritten.

## Candidate flow

1. Commit and tag an immutable version.
2. Build native artifacts and `release-manifest.json` from that exact tag.
3. Publish the assets as a GitHub prerelease.
4. Run fresh-install, update, rollback, persistence, and failure validation.
5. Promote the existing GitHub Release to Stable only after the release gate
   passes. Promotion does not change tags or assets.

## Update boundaries

The shared Update Manager validates and records release manifests, channel/pin
policy, plans, and history. WebUI, Discord, and CLI call that same API.

VirtizAI Core does not receive Docker-socket or root permissions. A platform
updater helper or external deployment mechanism applies a verified plan:

- Docker/Compose or Portainer performs image/container replacement.
- Native Linux uses a narrow helper to install a verified `.deb`, manage
  `virtizai.service`, and create/restore a VirtizAI-only backup.

## Reproducible Docker transition validation

On an isolated Docker validation host that has cloned this repository, run:

```sh
packaging/validate-compose-transition.sh 0.1.0 0.1.1
```

The script checks out the exact immutable tags, overrides legacy Compose build
metadata without modifying either tag, waits for `/healthz` to return the expected
version, verifies the OCI label, preserves named volumes across the full
`0.1.0 -> 0.1.1 -> 0.1.0` sequence, and tears the deployment down afterward.

## VM readiness

Before the Docker validation script is invoked on VM 121, verify startup with:

```sh
cloud-init status --wait
systemctl is-active ssh
```

This is the readiness gate used for the Phase 10 validation environment; no
arbitrary timing delay is required.

## Phase 11 boundary

Publishing GHCR images, signed provenance/attestations, generic Linux archives,
and automated release promotion are Phase 11 pipeline work. They remain required
before any candidate is promoted to Stable.
