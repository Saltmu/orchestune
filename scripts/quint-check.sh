#!/usr/bin/env bash
# Install and verify the pinned Quint toolchain (#1276).
#
#   Exit 0  Node.js matches package.json "engines" and the locked Quint is installed
#   Exit 2  Node.js / npm is missing or has the wrong major version (never skipped)
#   Exit 1  npm ci failed, or the installed Quint is not the pinned version
#
# Quint is never taken from a global install: `npm ci` installs exactly what
# package-lock.json locks into node_modules/, and the replay tests call that binary.
set -euo pipefail

# No external command may run before the Node.js check (the check must work on a
# bare PATH), so the script directory comes from bash itself.
script_path="${BASH_SOURCE[0]}"
script_dir="${script_path%/*}"
[ "${script_dir}" = "${script_path}" ] && script_dir="."
cd "${script_dir}/.."

install_help() {
  echo "ERROR: Node.js and npm are required for local CI (the Quint dependency-liveness check)." >&2
  echo "Install the Node.js major version listed under \"engines\" in package.json, then rerun." >&2
  echo "See CONTRIBUTING.md (Node.js and Quint) for the steps per OS." >&2
}

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  install_help
  exit 2
fi

# Prints "<node major> <required lower> <required upper>"; engines is ">=L <U".
read -r node_major lower upper < <(node -p '
  const m = require("./package.json").engines.node.match(/^>=(\d+) <(\d+)$/);
  if (!m) { throw new Error("package.json engines.node must look like \">=24 <25\""); }
  `${process.versions.node.split(".")[0]} ${m[1]} ${m[2]}`')
if [ "${node_major}" -lt "${lower}" ] || [ "${node_major}" -ge "${upper}" ]; then
  echo "ERROR: Node.js ${node_major} is installed; package.json requires >=${lower} <${upper}." >&2
  install_help
  exit 2
fi

pinned=$(node -p 'require("./package.json").devDependencies["@informalsystems/quint"]')
lock_hash=$(node -p 'require("crypto").createHash("sha256").update(require("fs").readFileSync("package-lock.json")).digest("hex")')
marker="node_modules/.quint-check-lock"
if [ ! -x node_modules/.bin/quint ] || [ "$(cat "${marker}" 2>/dev/null || true)" != "${lock_hash}" ]; then
  echo "Installing the locked Node.js tools (npm ci)..."
  npm ci --ignore-scripts --no-audit --no-fund
  printf '%s' "${lock_hash}" > "${marker}"
fi

installed=$(node_modules/.bin/quint --version)
if [ "${installed}" != "${pinned}" ]; then
  echo "ERROR: quint ${installed} is installed but package.json pins ${pinned}." >&2
  exit 1
fi
echo "Quint ${installed} (Node.js $(node --version)) is ready."
