#!/bin/sh
# Run on an isolated Docker validation host cloned from the VirtizAI repository.
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <base-tag> <target-tag>" >&2
    exit 64
fi

base_tag=$1
target_tag=$2
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose=${COMPOSE_COMMAND:-docker-compose}
override=$(mktemp)

cleanup() {
    VIRTIZAI_VERSION="$base_tag" "$compose" -f "$root/compose.yaml" -f "$override" down >/dev/null 2>&1 || true
    rm -f "$override"
}
trap cleanup EXIT INT TERM

cat > "$override" <<'EOF'
services:
  virtizai:
    build:
      args:
        VIRTIZAI_VERSION: ${VIRTIZAI_VERSION}
    image: virtizai:${VIRTIZAI_VERSION}
EOF

wait_for_health() {
    expected_version=$1
    EXPECTED_VERSION="$expected_version" python3 - <<'PY'
import json
import os
import time
from urllib.error import URLError
from urllib.request import urlopen

expected = os.environ["EXPECTED_VERSION"]
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        with urlopen("http://127.0.0.1:8766/healthz", timeout=3) as response:
            health = json.load(response)
        if health.get("status") == "ok" and health.get("version") == expected:
            print(json.dumps(health, sort_keys=True))
            raise SystemExit(0)
    except (URLError, TimeoutError, ValueError):
        pass
    time.sleep(0.25)
raise SystemExit(f"VirtizAI did not become healthy as {expected} within 60 seconds")
PY
}

run_release() {
    version=$1
    git -C "$root" checkout --detach "v$version"
    VIRTIZAI_VERSION="$version" "$compose" -f "$root/compose.yaml" -f "$override" up --build -d
    image_version=$(docker image inspect "virtizai:$version" --format '{{ index .Config.Labels "org.opencontainers.image.version" }}')
    test "$image_version" = "$version"
    wait_for_health "$version"
}

run_release "$base_tag"
run_release "$target_tag"
run_release "$base_tag"
