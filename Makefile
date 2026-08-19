VERSION := $(shell PYTHONPATH=. python3 -c 'from virtizai_core.version import __version__; print(__version__)')

.PHONY: test build package docker compose-up checksums release-manifest clean

test:
	python3 -m pytest -q tests

build:
	python3 -m compileall -q virtizai_core tests webui virtizai_cli.py

package: build
	sh packaging/build-deb.sh

docker:
	docker build --build-arg VIRTIZAI_VERSION=$(VERSION) -t virtizai:$(VERSION) .

compose-up:
	VIRTIZAI_VERSION=$(VERSION) docker compose up --build -d

checksums: package
	sha256sum dist/* > dist/SHA256SUMS

release-manifest: package
	python3 packaging/generate-release-manifest.py --version $(VERSION) --release-url https://github.com/virtiz/VirtizAI/releases/tag/v$(VERSION) --deb dist/virtizai_$(VERSION)_amd64.deb --output dist/release-manifest.json

clean:
	rm -rf dist build *.egg-info
