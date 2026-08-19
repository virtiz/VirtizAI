PYTHON := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
VERSION := $(shell PYTHONPATH=. $(PYTHON) -c 'from virtizai_core.version import __version__; print(__version__)')
MIN_ROLLBACK_VERSION := $(shell PYTHONPATH=. $(PYTHON) -c 'from virtizai_core.version import MIN_MANAGED_ROLLBACK_VERSION; print(MIN_MANAGED_ROLLBACK_VERSION)')
TARGET_SCHEMA := $(shell PYTHONPATH=. $(PYTHON) -c 'from virtizai_core.migrations import MIGRATIONS; print(MIGRATIONS[-1][0])')

.PHONY: test build package docker compose-up checksums release-manifest clean

test:
	$(PYTHON) -m pytest -q tests

build:
	$(PYTHON) -m compileall -q virtizai_core tests webui benchmarks virtizai_cli.py

package: build
	sh packaging/build-deb.sh

docker:
	docker build --build-arg VIRTIZAI_VERSION=$(VERSION) -t virtizai:$(VERSION) .

compose-up:
	VIRTIZAI_VERSION=$(VERSION) docker compose up --build -d

checksums: package
	sha256sum dist/* > dist/SHA256SUMS

release-manifest: package
	$(PYTHON) packaging/generate-release-manifest.py --version $(VERSION) --release-url https://github.com/virtiz/VirtizAI/releases/tag/v$(VERSION) --deb dist/virtizai_$(VERSION)_amd64.deb --output dist/release-manifest.json --target-schema $(TARGET_SCHEMA) --minimum-upgrade-version $(MIN_ROLLBACK_VERSION) --minimum-managed-rollback-version $(MIN_ROLLBACK_VERSION) --rollback-baseline $(MIN_ROLLBACK_VERSION) --rollback-requires-data-restore

clean:
	rm -rf dist build *.egg-info
