# VirtizAI releases

Git tags and published assets are immutable. Each release is built from its exact tagged commit and published with a checksum-bound manifest.

## Versioning and channels

VirtizAI uses semantic versions. Published versions may be prereleases while they receive validation. Stable, beta, and other channels are selected through release metadata and update policy; a prerelease is never silently treated as Stable.

## Automated release pipeline

Pull requests and changes to `main` run the full test suite, application/package build, package metadata checks, and a non-publishing container build.

Pushing a new immutable `v<version>` tag starts the release workflow. Only the tag workflow has package/image publishing permissions. It validates that the tag exactly matches the application version, builds the `.deb` and container from that commit, publishes the container to GHCR, generates the machine-readable manifest and checksums, creates GitHub provenance attestations, and attaches the artifacts to the GitHub Release. Forks and pull requests cannot access release credentials.

## Release flow

1. Prepare and review a clean commit.
2. Create a new immutable `v<major>.<minor>.<patch>` tag.
3. Build the native package and manifest from that tag.
4. Publish the exact package, manifest, and SHA-256 checksum.
5. Validate installation, upgrades, rollback, persistence, and failure recovery.
6. Promote only after all required gates pass. Promotion never rewrites a tag or asset.

## Verification and compatibility

Manifests identify version, channel, artifact URL, SHA-256, target schema, minimum upgrade version, and rollback compatibility. The Update Manager verifies them before managed operations. Native installation uses a constrained privileged helper while the application remains unprivileged.

Schema-changing updates declare compatibility boundaries. Application-only rollback is refused when unsafe; supported data-restoring rollback validates backup path, checksum, metadata, and archive contents before replacing SQLite state. Docker/Compose owns container replacement. Unmanaged transitions are recorded as external updates without claiming a manager-created backup.

GitHub Releases and GHCR remain authoritative; a website is optional and never participates in installation or update discovery. See [DEPLOYMENT.md](DEPLOYMENT.md) and [docs/development.md](docs/development.md).
