#!/usr/bin/env bash
# Install the pinned Foldseek static Linux build into tools/bin/.
#
# Foldseek is the only downloaded executable used by lifesci. The archive is
# pinned by URL + SHA-256 and the binary version is asserted after extraction;
# there is no silent auto-update (DESIGN §2.7/§4.7).
#
#   ./scripts/install_foldseek.sh            # install/verify the pinned build
#   ./scripts/install_foldseek.sh --force    # reinstall even if present
#
# Environment: LIFESCI_FOLDSEEK_DIR overrides the install directory (default: <repo>/tools/bin).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${LIFESCI_FOLDSEEK_DIR:-$REPO_ROOT/tools/bin}"

# --- pinned release ---------------------------------------------------------
FOLDSEEK_VERSION="941cd33ff0771cd2e3f144e3293e22a2b87e9fda"
ARCHIVE_URL="https://github.com/steineggerlab/foldseek/releases/latest/download/foldseek-linux-avx2.tar.gz"
ARCHIVE_SHA256="e6a3e3860e76b0b7b858736f627fe680d4d7f0fc7298301d8a92b9f711d74ac7"
BINARY_SHA256="4db94a7825d409e416643772860ddf7cfe328d582679b47e6dffda7df4d41ea0"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then FORCE=1; fi

if [[ -x "$INSTALL_DIR/foldseek" && $FORCE -eq 0 ]]; then
  have="$("$INSTALL_DIR/foldseek" version 2>/dev/null | head -1 || true)"
  if [[ "$have" == "$FOLDSEEK_VERSION" ]]; then
    echo "foldseek already installed: $INSTALL_DIR/foldseek ($have)"
    exit 0
  fi
  echo "foldseek present but version '$have' != pinned '$FOLDSEEK_VERSION'; reinstalling"
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "downloading $ARCHIVE_URL"
curl -sSL --fail --retry 3 --max-time 600 -o "$TMP_DIR/foldseek.tar.gz" "$ARCHIVE_URL"

actual="$(sha256sum "$TMP_DIR/foldseek.tar.gz" | cut -d' ' -f1)"
if [[ "$actual" != "$ARCHIVE_SHA256" ]]; then
  echo "ERROR: archive SHA-256 mismatch" >&2
  echo "  expected $ARCHIVE_SHA256" >&2
  echo "  actual   $actual" >&2
  echo "Refusing to install an unverified binary." >&2
  exit 1
fi

tar xzf "$TMP_DIR/foldseek.tar.gz" -C "$TMP_DIR"
BIN="$TMP_DIR/foldseek/bin/foldseek"
[[ -f "$BIN" ]] || { echo "ERROR: extracted archive has no foldseek/bin/foldseek" >&2; exit 1; }

bin_actual="$(sha256sum "$BIN" | cut -d' ' -f1)"
if [[ "$bin_actual" != "$BINARY_SHA256" ]]; then
  echo "ERROR: binary SHA-256 mismatch" >&2
  echo "  expected $BINARY_SHA256" >&2
  echo "  actual   $bin_actual" >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR"
install -m 0755 "$BIN" "$INSTALL_DIR/foldseek"
installed_version="$("$INSTALL_DIR/foldseek" version | head -1)"
if [[ "$installed_version" != "$FOLDSEEK_VERSION" ]]; then
  echo "ERROR: installed binary reports version '$installed_version', expected '$FOLDSEEK_VERSION'" >&2
  exit 1
fi

echo "installed foldedseek $installed_version -> $INSTALL_DIR/foldseek"
echo "verify with: $INSTALL_DIR/foldseek version && python -m lifesci doctor --input '{}'"
