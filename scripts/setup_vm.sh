#!/usr/bin/env bash
#
# Prepare a fresh VM for a se-lab-based product lab.
#
# Ported from m3undle-lab's scripts/common/setup_vm.sh, generalized. That
# version undersold as a "prefix strip" the same way common.py/cli.py did
# before their own audits -- it hardcoded the M3Undle product name into a
# directory-naming convention that no longer matches what agent/common.py
# actually computes (default_runtime_dir() is a uniform {product_name}, not
# the original's asymmetric "m3undle" for srv1 / "client-apps" for srv2),
# installed a product-specific apt package (hdhomerun-config) unconditionally,
# pre-created NextPVR-specific directories, and only ever installed a bare
# `python3` with no venv -- exactly the two gaps the earlier bootstrap audit
# flagged as working only by accident on Ubuntu 24.04. All fixed here; see
# .ai_docs/roadmap.md.
#
# The original also had a --role srv1|srv2 concept (one physical server ran
# the automated suites, a second ran client-app checklists). Removed here --
# see .ai_docs/roadmap.md's role-removal audit -- once a product lab deploys
# to one server, "role" has no meaning left to select.
#
# Unlike the `lab` shim / scripts/agent.py, this script does NOT need to
# self-locate: it operates entirely on --base-dir/--lab-dir and explicit
# --product-name/--env-prefix arguments, so it can live in se-lab itself
# and be invoked directly, not copied out as a template.

set -euo pipefail

BASE_DIR="/opt"
LAB_DIR=""
PRODUCT_NAME=""
ENV_PREFIX=""
OWNER="${SUDO_USER:-${USER}}"
GROUP="${OWNER}"
INSTALL_DEPS=1
EXTRA_PACKAGES=""
PYTHON_VERSION="3.12"

usage() {
  cat <<'EOF'
Usage: bash scripts/setup_vm.sh --product-name NAME --env-prefix PREFIX
         [--base-dir /opt] [--lab-dir PATH]
         [--owner USER] [--group GROUP] [--extra-packages "pkg1 pkg2"]
         [--skip-install-deps]

Prepare a fresh VM for a se-lab-based product lab:
- install git, python3.12 (+venv), docker, Docker Compose, Docker Buildx on
  Ubuntu/Debian, plus any --extra-packages your product needs
- create the lab's own .venv and install its Python dependencies into it
- create required directories, set ownership
- create lab.env if missing
- prepare the runtime compose layout
- enable Docker and add the selected owner to the docker group
- run a preflight check and report PASS/FAIL for each requirement

--product-name and --env-prefix are required and must match what your
scripts/agent.py passes to agent.runtime.configure() -- see
docs/templates/scripts/agent.py.

Run with sudo when writing under /opt.
EOF
}

run_cmd() {
  printf '+'
  for arg in "$@"; do
    printf ' %q' "$arg"
  done
  printf '\n'
  "$@"
}

have_compose_v2() {
  docker compose version >/dev/null 2>&1
}

have_buildx() {
  docker buildx version >/dev/null 2>&1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "This script must run as root to install packages." >&2
    echo "Run: sudo bash scripts/setup_vm.sh ..." >&2
    echo "" >&2
    echo "If you are an automated agent without a way to supply a sudo password" >&2
    echo "interactively: STOP here rather than retrying or attempting a workaround." >&2
    echo "sudo will hang waiting for input in a non-interactive session with no" >&2
    echo "cached credential -- it will not fail cleanly, it will just not return." >&2
    echo "Ask the operator to run the sudo command above themselves. If git," >&2
    echo "python${PYTHON_VERSION}, docker, compose, and buildx are already installed," >&2
    echo "rerun this script with --skip-install-deps instead -- that path does not" >&2
    echo "require root as long as ${LAB_DIR:-the lab directory} is already owned by" >&2
    echo "the invoking user." >&2
    exit 1
  fi
}

# Only chown when it would actually change something -- an unprivileged user
# cannot chown at all (even a same-owner no-op) without root/CAP_CHOWN on
# Linux, so calling this unconditionally made --skip-install-deps still
# require root in practice whenever the directory was already correctly
# owned (the common case for an agent working in its own checkout).
chown_if_needed() {
  local owner_group="$1"
  shift
  local path
  for path in "$@"; do
    if [[ "$(stat -c '%U:%G' "${path}" 2>/dev/null)" != "${owner_group}" ]]; then
      run_cmd chown -R "${owner_group}" "${path}"
    fi
  done
}

is_ubuntu_like() {
  if [[ ! -f /etc/os-release ]]; then
    return 1
  fi
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" || "${ID:-}" == "debian" || "${ID_LIKE:-}" == *"debian"* ]]
}

install_first_available_package() {
  local package
  for package in "$@"; do
    if apt-cache show "${package}" >/dev/null 2>&1; then
      run_cmd apt-get install -y "${package}"
      return 0
    fi
  done
  echo "Unable to find any of these packages in APT: $*" >&2
  exit 1
}

python_available() {
  command -v "python${PYTHON_VERSION}" >/dev/null 2>&1
}

install_python() {
  if python_available; then
    return 0
  fi
  echo "python${PYTHON_VERSION} is not available from this distro's default APT sources." >&2
  echo "Adding the deadsnakes PPA to get it..." >&2
  run_cmd apt-get install -y software-properties-common
  run_cmd add-apt-repository -y ppa:deadsnakes/ppa
  run_cmd apt-get update
}

install_dependencies() {
  if [[ "${INSTALL_DEPS}" -ne 1 ]]; then
    return 0
  fi
  require_root
  if ! is_ubuntu_like; then
    echo "Automatic dependency installation is supported only on Ubuntu/Debian right now." >&2
    echo "Rerun with --skip-install-deps after installing git, python${PYTHON_VERSION}" \
         "(+venv), docker, Docker Compose, and Docker Buildx manually." >&2
    exit 1
  fi

  export DEBIAN_FRONTEND=noninteractive
  run_cmd apt-get update
  install_python
  # shellcheck disable=SC2086 # EXTRA_PACKAGES is an intentionally word-split list
  run_cmd apt-get install -y ca-certificates curl git net-tools docker.io \
    "python${PYTHON_VERSION}" "python${PYTHON_VERSION}-venv" ${EXTRA_PACKAGES}
  install_first_available_package docker-compose-v2 docker-compose-plugin
  install_first_available_package docker-buildx docker-buildx-plugin

  if command -v systemctl >/dev/null 2>&1; then
    run_cmd systemctl enable --now docker
  fi
  if getent group docker >/dev/null 2>&1; then
    run_cmd usermod -aG docker "${OWNER}"
  fi
}

set_env_value() {
  local file="$1" key="$2" value="$3"
  python3 - "$file" "$key" "$value" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]

lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
updated = False
out = []
for line in lines:
    if line.startswith(f"{key}="):
        out.append(f"{key}={value}")
        updated = True
    else:
        out.append(line)
if not updated:
    out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --product-name) PRODUCT_NAME="$2"; shift 2 ;;
    --env-prefix) ENV_PREFIX="$2"; shift 2 ;;
    --base-dir) BASE_DIR="$2"; shift 2 ;;
    --lab-dir) LAB_DIR="$2"; shift 2 ;;
    --owner) OWNER="$2"; shift 2 ;;
    --group) GROUP="$2"; shift 2 ;;
    --extra-packages) EXTRA_PACKAGES="$2"; shift 2 ;;
    --skip-install-deps) INSTALL_DEPS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${PRODUCT_NAME}" || -z "${ENV_PREFIX}" ]]; then
  echo "Both --product-name and --env-prefix are required." >&2
  usage >&2
  exit 1
fi

if [[ -z "${LAB_DIR}" ]]; then
  LAB_DIR="${BASE_DIR}/${PRODUCT_NAME}-lab"
fi

if [[ ! -d "${LAB_DIR}" ]]; then
  echo "Expected lab repo at ${LAB_DIR}. Clone it (with --recurse-submodules) first, then rerun this script." >&2
  exit 1
fi
if [[ ! -d "${LAB_DIR}/se-lab" ]]; then
  echo "No se-lab/ submodule found at ${LAB_DIR}/se-lab. Run 'git submodule update --init' first." >&2
  exit 1
fi

install_dependencies

for command in "python${PYTHON_VERSION}" git docker; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "Missing required command: ${command}" >&2
    exit 1
  fi
done
if ! have_compose_v2; then
  echo "Missing Docker Compose v2. Install a working 'docker compose' plugin." >&2
  exit 1
fi
if ! have_buildx; then
  echo "Missing Docker Buildx. Install a working 'docker buildx' plugin." >&2
  exit 1
fi

RUNTIME_DIR="${BASE_DIR}/${PRODUCT_NAME}"

# Same reasoning as chown_if_needed(): mkdir -p on paths that already exist
# is a no-op and needs no privilege at all. Only the genuinely-missing ones
# need creating, and creating them may need root if their parent (e.g. a
# root-owned /opt) isn't writable by the invoking user -- fail with the same
# actionable hint as require_root() in that case, not a raw mkdir error.
missing_dirs=()
for dir in "${RUNTIME_DIR}" "${LAB_DIR}/docker-config" "${LAB_DIR}/repos" "${LAB_DIR}/fixtures" "${LAB_DIR}/results"; do
  [[ -d "${dir}" ]] || missing_dirs+=("${dir}")
done
if [[ "${#missing_dirs[@]}" -gt 0 ]]; then
  if ! run_cmd mkdir -p "${missing_dirs[@]}" 2>/dev/null; then
    echo "Could not create: ${missing_dirs[*]}" >&2
    echo "This needs root if ${BASE_DIR} isn't writable by the invoking user." >&2
    echo "See the sudo guidance printed by the missing-package check above (or" >&2
    echo "rerun as root/sudo if this is the first thing to fail)." >&2
    exit 1
  fi
fi
chown_if_needed "${OWNER}:${GROUP}" "${RUNTIME_DIR}" "${LAB_DIR}"

if [[ ! -f "${LAB_DIR}/lab.env" ]]; then
  if [[ -f "${LAB_DIR}/lab.env.example" ]]; then
    run_cmd cp "${LAB_DIR}/lab.env.example" "${LAB_DIR}/lab.env"
    echo "Created ${LAB_DIR}/lab.env from lab.env.example."
  else
    run_cmd touch "${LAB_DIR}/lab.env"
    echo "Created empty ${LAB_DIR}/lab.env."
  fi
fi
set_env_value "${LAB_DIR}/lab.env" "${ENV_PREFIX}_RUNTIME_DIR" "${RUNTIME_DIR}"
chown_if_needed "${OWNER}:${GROUP}" "${LAB_DIR}/lab.env"

echo "Creating .venv and installing dependencies..."
run_cmd "python${PYTHON_VERSION}" -m venv "${LAB_DIR}/.venv"
run_cmd "${LAB_DIR}/.venv/bin/pip" install --upgrade pip
run_cmd "${LAB_DIR}/.venv/bin/pip" install -r "${LAB_DIR}/se-lab/requirements.txt"
chown_if_needed "${OWNER}:${GROUP}" "${LAB_DIR}/.venv"

echo "Preparing runtime compose layout..."
"${LAB_DIR}/.venv/bin/python" - "${LAB_DIR}" "${PRODUCT_NAME}" "${ENV_PREFIX}" <<'PY'
import sys
from pathlib import Path
lab_dir, product_name, env_prefix = sys.argv[1:4]
sys.path.insert(0, f"{lab_dir}/se-lab")
from agent.runtime import configure
configure(repo_root=Path(lab_dir), product_name=product_name, env_prefix=env_prefix)
from agent import common
common.ensure_layout()
common.sync_runtime_compose()
print(f"Prepared {common.runtime_summary()}.")
print(f"Runtime env file path: {common.runtime_env_file()}.")
if common.runtime_compose_file().exists():
    print(f"Compose will run from {common.runtime_compose_file()}.")
else:
    print("No compose template is registered yet.")
PY

echo ""
echo "--- Preflight ---"
preflight_failed=0
check() {
  local label="$1" ok="$2" detail="$3"
  if [[ "${ok}" -eq 0 ]]; then
    printf '  %-28s PASS  %s\n' "${label}" "${detail}"
  else
    printf '  %-28s FAIL  %s\n' "${label}" "${detail}"
    preflight_failed=1
  fi
}

py_version="$("${LAB_DIR}/.venv/bin/python" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
check "python (.venv)" 0 "${py_version}"
if "${LAB_DIR}/.venv/bin/python" -c "import yaml" >/dev/null 2>&1; then
  check "PyYAML" 0 "importable"
else
  check "PyYAML" 1 "not importable in .venv"
fi
if "${LAB_DIR}/.venv/bin/python" -c "import requests" >/dev/null 2>&1; then
  check "requests" 0 "importable"
else
  check "requests" 1 "not importable in .venv"
fi
if have_compose_v2; then
  check "docker compose v2" 0 "$(docker compose version --short 2>/dev/null || echo present)"
else
  check "docker compose v2" 1 "missing"
fi
if have_buildx; then
  check "docker buildx" 0 "present"
else
  check "docker buildx" 1 "missing"
fi

# Only actually invoke sudo when switching user (running as root, checking a
# different target user). When already running as OWNER (the non-root path
# this whole script now supports), check directly -- sudo -u samecurrentuser
# still goes through sudo's auth path and can hang non-interactively for no
# reason, same failure shape as require_root() above. -n makes the root case
# fail fast instead of hanging too, rather than silently blocking forever.
if [[ "${EUID}" -eq 0 ]]; then
  docker_check=(sudo -n -u "${OWNER}" docker info)
else
  docker_check=(docker info)
fi
if "${docker_check[@]}" >/dev/null 2>&1; then
  check "docker socket as ${OWNER}" 0 "usable now"
else
  check "docker socket as ${OWNER}" 1 "not usable yet -- log out and back in"
fi
echo "-----------------"
if [[ "${preflight_failed}" -ne 0 ]]; then
  echo "One or more preflight checks failed -- see above." >&2
fi

echo ""
echo "Lab environment prepared under ${BASE_DIR}."
echo "Next steps:"
echo "  1. Edit ${LAB_DIR}/lab.env with your site-specific values."
echo "  2. cd ${LAB_DIR} && ./lab discover"
echo "  (see your product lab's own README/AGENTS.md for available commands)"
exit "${preflight_failed}"
