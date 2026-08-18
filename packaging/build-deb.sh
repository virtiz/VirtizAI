#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(PYTHONPATH="$ROOT" python3 -c 'from virtizai_core.version import __version__; print(__version__)')
ARCH=${DEB_ARCH:-amd64}
OUT=${OUT_DIR:-"$ROOT/dist"}
PKG="$OUT/virtizai_${VERSION}_${ARCH}"
rm -rf "$PKG" "$OUT/virtizai_${VERSION}_${ARCH}.deb"
mkdir -p "$PKG/DEBIAN" "$PKG/usr/lib/virtizai" "$PKG/etc/virtizai" "$PKG/var/lib/virtizai/workspace" "$PKG/var/log/virtizai"
cp "$ROOT/pyproject.toml" "$ROOT/requirements-runtime.txt" "$ROOT/README.md" "$ROOT/virtizai_cli.py" "$PKG/usr/lib/virtizai/"
cp -R "$ROOT/virtizai_core" "$ROOT/webui" "$PKG/usr/lib/virtizai/"
cp "$ROOT/packaging/systemd/virtizai.service" "$PKG/libvirtizai.service.tmp"
mkdir -p "$PKG/lib/systemd/system"
mv "$PKG/libvirtizai.service.tmp" "$PKG/lib/systemd/system/virtizai.service"
cat > "$PKG/DEBIAN/control" <<EOF
Package: virtizai
Version: $VERSION
Section: net
Priority: optional
Architecture: $ARCH
Depends: python3 (>= 3.11), python3-venv, ca-certificates
Maintainer: VirtizAI
Description: Self-hosted AI orchestration core
 Independent WebUI, provider routing, tools, and execution core.
EOF
cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -eu
if ! id virtizai >/dev/null 2>&1; then useradd --system --home-dir /var/lib/virtizai --shell /usr/sbin/nologin virtizai; fi
python3 -m venv /usr/lib/virtizai/venv
/usr/lib/virtizai/venv/bin/pip install --no-cache-dir -r /usr/lib/virtizai/requirements-runtime.txt
chown -R virtizai:virtizai /var/lib/virtizai /var/log/virtizai /etc/virtizai
systemctl daemon-reload
systemctl enable virtizai.service
systemctl restart virtizai.service || true
EOF
chmod 755 "$PKG/DEBIAN/postinst"
cat > "$PKG/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -eu
systemctl stop virtizai.service || true
EOF
chmod 755 "$PKG/DEBIAN/prerm"
mkdir -p "$OUT"
DEB_FILE="$OUT/virtizai_${VERSION}_${ARCH}.deb"
if command -v dpkg-deb >/dev/null 2>&1; then
	dpkg-deb --build --root-owner-group "$PKG" "$DEB_FILE"
else
	# Minimal Debian archive fallback for non-Debian build hosts.
	TMP=$(mktemp -d)
	printf '2.0\n' > "$TMP/debian-binary"
	tar -C "$PKG/DEBIAN" --owner=0 --group=0 --numeric-owner -czf "$TMP/control.tar.gz" .
	tar -C "$PKG" --exclude=DEBIAN --owner=0 --group=0 --numeric-owner -czf "$TMP/data.tar.gz" .
	(cd "$TMP" && ar r "$DEB_FILE" debian-binary control.tar.gz data.tar.gz >/dev/null)
	rm -rf "$TMP"
fi
sha256sum "$DEB_FILE" > "$DEB_FILE.sha256"
printf '%s\n' "$OUT/virtizai_${VERSION}_${ARCH}.deb"
