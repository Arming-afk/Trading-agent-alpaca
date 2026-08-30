#!/usr/bin/env bash
# Install Alpaca's official CLI (github.com/alpacahq/cli) — the only Alpaca
# client this project uses. Verifies the published checksum before installing.
set -euo pipefail

VERSION="${ALPACA_CLI_VERSION:-0.0.14}"
DEST="${ALPACA_CLI_DEST:-$HOME/.local/bin}"

case "$(uname -s)" in
  Darwin) OS=darwin ;;
  Linux)  OS=linux ;;
  *) echo "unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  arm64|aarch64) ARCH=arm64 ;;
  x86_64|amd64)  ARCH=amd64 ;;
  *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac

TARBALL="cli_${VERSION}_${OS}_${ARCH}.tar.gz"
BASE="https://github.com/alpacahq/cli/releases/download/v${VERSION}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "→ downloading ${TARBALL}"
curl -fsSL -o "$TMP/$TARBALL" "$BASE/$TARBALL"
curl -fsSL -o "$TMP/checksums.txt" "$BASE/checksums.txt"

echo "→ verifying checksum"
EXPECTED="$(grep "  ${TARBALL}\$" "$TMP/checksums.txt" | awk '{print $1}')"
if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL="$(sha256sum "$TMP/$TARBALL" | awk '{print $1}')"
else
  ACTUAL="$(shasum -a 256 "$TMP/$TARBALL" | awk '{print $1}')"
fi
if [ -z "$EXPECTED" ] || [ "$EXPECTED" != "$ACTUAL" ]; then
  echo "checksum mismatch — refusing to install" >&2
  echo "  expected: ${EXPECTED:-<not found>}" >&2
  echo "  actual:   $ACTUAL" >&2
  exit 1
fi

tar xzf "$TMP/$TARBALL" -C "$TMP"
mkdir -p "$DEST"
install -m 755 "$TMP/alpaca" "$DEST/alpaca"

echo "✓ installed alpaca CLI v${VERSION} → $DEST/alpaca"
echo
echo "  Add to PATH if needed:  export PATH=\"$DEST:\$PATH\""
echo "  Then verify:            alpaca doctor"
