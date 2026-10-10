#!/bin/bash
# SessionStart hook for Claude Code on the web.
# Installs the toolchain CONTRIBUTING.md expects (uv deps on Python 3.12,
# Node.js + the locked Quint, git hooks + gitleaks, GitHub CLI) so
# tests/lint/gh and the local CI work from the first turn.
#
# Env overrides (used by tests/test_session_start_hook.py):
#   ORCHESTUNE_NODE_VERSION    pinned Node.js release (default below)
#   ORCHESTUNE_NODE_DIST_BASE  distribution base URL (default https://nodejs.org/dist)
#   ORCHESTUNE_NODE_LIB_DIR    where the release is unpacked
#   ORCHESTUNE_NODE_BIN_DIR    where node / npm / npx are linked
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# --- Python deps (pyproject.toml requires Python 3.12+) ---
if command -v uv >/dev/null 2>&1; then
  uv sync
fi

# --- Node.js + the locked Quint (required by the local CI, #1276) ---
# Node.js is installed from the official distribution when it is missing or its
# major version is outside package.json "engines"; the archive is verified against
# the release's SHASUMS256.txt (same approach as scripts/install-gitleaks.sh).
# A failure here must not stop the rest of the setup, but it must not pass
# silently either: the hook finishes the other steps and then exits non-zero.
NODE_VERSION="${ORCHESTUNE_NODE_VERSION:-v24.21.0}"
NODE_DIST_BASE="${ORCHESTUNE_NODE_DIST_BASE:-https://nodejs.org/dist}"

node_major_ok() {
  local lower upper major
  read -r lower upper < <(sed -n 's/.*"node": *">=\([0-9][0-9]*\) <\([0-9][0-9]*\)".*/\1 \2/p' package.json)
  [ -n "${lower:-}" ] || { echo "ERROR: package.json engines.node must look like \">=24 <25\"." >&2; return 2; }
  command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 || return 1
  major="$(node --version | sed -n 's/^v\([0-9][0-9]*\)\..*/\1/p')"
  [ -n "$major" ] && [ "$major" -ge "$lower" ] && [ "$major" -lt "$upper" ]
}

fetch_node() {
  local tmp_dir="$1" archive="$2"
  local expected actual
  echo "Installing Node.js ${NODE_VERSION} (${archive})..."
  curl -fsSL --retry 3 --retry-delay 2 "${NODE_DIST_BASE}/${NODE_VERSION}/${archive}" -o "${tmp_dir}/${archive}" \
    || { echo "ERROR: could not download ${archive} from ${NODE_DIST_BASE}." >&2; return 1; }
  curl -fsSL --retry 3 --retry-delay 2 "${NODE_DIST_BASE}/${NODE_VERSION}/SHASUMS256.txt" -o "${tmp_dir}/SHASUMS256.txt" \
    || { echo "ERROR: could not download SHASUMS256.txt from ${NODE_DIST_BASE}." >&2; return 1; }
  expected="$(grep " ${archive}\$" "${tmp_dir}/SHASUMS256.txt" | awk '{print $1}')"
  [ -n "$expected" ] || { echo "ERROR: no checksum for ${archive} in SHASUMS256.txt." >&2; return 1; }
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "${tmp_dir}/${archive}" | awk '{print $1}')"
  else
    actual="$(shasum -a 256 "${tmp_dir}/${archive}" | awk '{print $1}')"
  fi
  if [ "$expected" != "$actual" ]; then
    echo "ERROR: Checksum mismatch for ${archive} (expected ${expected}, got ${actual})." >&2
    return 1
  fi
}

link_node() {
  local tmp_dir="$1" archive="$2" arch="$3"
  local lib_dir bin_dir
  if [ "$(id -u)" = "0" ]; then
    lib_dir="${ORCHESTUNE_NODE_LIB_DIR:-/usr/local/lib/nodejs}"
    bin_dir="${ORCHESTUNE_NODE_BIN_DIR:-/usr/local/bin}"
  else
    lib_dir="${ORCHESTUNE_NODE_LIB_DIR:-$HOME/.local/lib/nodejs}"
    bin_dir="${ORCHESTUNE_NODE_BIN_DIR:-$HOME/.local/bin}"
  fi
  mkdir -p "$lib_dir" "$bin_dir"
  tar -xzf "${tmp_dir}/${archive}" --no-same-owner -C "$lib_dir"
  for tool in node npm npx; do
    ln -sf "${lib_dir}/node-${NODE_VERSION}-linux-${arch}/bin/${tool}" "${bin_dir}/${tool}"
  done
  export PATH="${bin_dir}:${PATH}"
  # Later commands of the session run in other shells: persist PATH when we can.
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "export PATH=\"${bin_dir}:\$PATH\"" >> "$CLAUDE_ENV_FILE"
  fi
}

install_node() {
  local arch archive tmp_dir status=0
  case "$(uname -m)" in
    x86_64 | amd64) arch="x64" ;;
    arm64 | aarch64) arch="arm64" ;;
    *) echo "ERROR: Node.js cannot be installed automatically on $(uname -m)." >&2; return 1 ;;
  esac
  archive="node-${NODE_VERSION}-linux-${arch}.tar.gz"
  tmp_dir="$(mktemp -d)"
  { fetch_node "$tmp_dir" "$archive" && link_node "$tmp_dir" "$archive" "$arch"; } || status=$?
  rm -rf "$tmp_dir"
  return "$status"
}

setup_node() {
  local status=0
  node_major_ok || status=$?
  if [ "$status" = "2" ]; then return 2; fi
  if [ "$status" != "0" ]; then
    install_node || return 1
    node_major_ok || { echo "ERROR: the installed Node.js does not satisfy package.json engines." >&2; return 1; }
  fi
  ./scripts/quint-check.sh
}

node_status=0
setup_node || node_status=$?

# --- Git hooks + gitleaks (idempotent) ---
if [ -x ./scripts/setup-git-hooks.sh ]; then
  ./scripts/setup-git-hooks.sh
fi

# --- GitHub CLI ---
if ! command -v gh >/dev/null 2>&1; then
  keyring=/etc/apt/keyrings/githubcli-archive-keyring.gpg
  mkdir -p -m 755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$keyring"
  chmod go+r "$keyring"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=$keyring] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update
  apt-get install -y gh
fi

if [ "$node_status" != "0" ]; then
  echo "ERROR: Node.js / Quint setup failed (exit ${node_status}); the local CI will stop until it is fixed." >&2
  exit "$node_status"
fi
