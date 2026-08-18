VERSION := $(shell PYTHONPATH=. python3 -c 'from virtizai_core.version import __version__; print(__version__)')

.PHONY: test build package docker checksums clean

test:
	python3 -m pytest -q tests

build:
	python3 -m compileall -q virtizai_core tests webui virtizai_cli.py

package: build
	sh packaging/build-deb.sh

docker:
	docker build --build-arg VIRTIZAI_VERSION=$(VERSION) -t virtizai:$(VERSION) .

checksums: package
	sha256sum dist/* > dist/SHA256SUMS

clean:
	rm -rf dist build *.egg-info
