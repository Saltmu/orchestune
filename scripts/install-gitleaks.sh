#!/usr/bin/env bash
# Installs the `gitleaks` binary locally if it is not already on PATH.
#
# Used by scripts/local-ci.sh (auto-recovery before the secret scan step) and
# scripts/setup-git-hooks.sh (proactive install during initial dev setup), so
# that fresh containers/environments don't need a manual gitleaks install
# before the mandatory local CI gitleaks scan can run.
#
# Env overrides:
#   GITLEAKS_VERSION      - pinned gitleaks release to install (default below)
#   GITLEAKS_INSTALL_DIR  - install destination (default: $HOME/.local/bin)
set -euo pipefail

GITLEAKS_VERSION="${GITLEAKS_VERSION:-8.30.1}"
INSTALL_DIR="${GITLEAKS_INSTALL_DIR:-$HOME/.local/bin}"

if command -v gitleaks >/dev/null 2>&1; then
  echo "gitleaks is already installed: $(command -v gitleaks)"
  exit 0
fi

os="$(uname -s)"
arch="$(uname -m)"

case "$os" in
  Linux) os_name="linux" ;;
  Darwin) os_name="darwin" ;;
  *)
    echo "ERROR: Automatic gitleaks installation is not supported on OS '$os'." >&2
    echo "Install it manually: https://github.com/gitleaks/gitleaks#installing" >&2
    exit 1
    ;;
esac

case "$arch" in
  x86_64 | amd64) arch_name="x64" ;;
  arm64 | aarch64) arch_name="arm64" ;;
  *)
    echo "ERROR: Automatic gitleaks installation is not supported on architecture '$arch'." >&2
    echo "Install it manually: https://github.com/gitleaks/gitleaks#installing" >&2
    exit 1
    ;;
esac

archive="gitleaks_${GITLEAKS_VERSION}_${os_name}_${arch_name}.tar.gz"
base_url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

echo "Installing gitleaks v${GITLEAKS_VERSION} (${os_name}/${arch_name}) to ${INSTALL_DIR}..."
curl -fsSL "${base_url}/${archive}" -o "${tmp_dir}/${archive}"
curl -fsSL "${base_url}/gitleaks_${GITLEAKS_VERSION}_checksums.txt" -o "${tmp_dir}/checksums.txt"

expected_checksum="$(grep " ${archive}\$" "${tmp_dir}/checksums.txt" | awk '{print $1}')"
if [ -z "$expected_checksum" ]; then
  echo "ERROR: Could not find checksum for ${archive} in checksums.txt." >&2
  exit 1
fi

actual_checksum="$(sha256sum "${tmp_dir}/${archive}" | awk '{print $1}')"
if [ "$expected_checksum" != "$actual_checksum" ]; then
  echo "ERROR: Checksum mismatch for ${archive}." >&2
  echo "  expected: ${expected_checksum}" >&2
  echo "  actual:   ${actual_checksum}" >&2
  exit 1
fi

tar -xzf "${tmp_dir}/${archive}" -C "${tmp_dir}" gitleaks
mkdir -p "${INSTALL_DIR}"
install -m 0755 "${tmp_dir}/gitleaks" "${INSTALL_DIR}/gitleaks"

echo "gitleaks installed at ${INSTALL_DIR}/gitleaks"

case ":${PATH}:" in
  *":${INSTALL_DIR}:"*) ;;
  *)
    echo "NOTE: ${INSTALL_DIR} is not currently on PATH." >&2
    echo "  Add it, e.g.: export PATH=\"${INSTALL_DIR}:\$PATH\"" >&2
    ;;
esac
