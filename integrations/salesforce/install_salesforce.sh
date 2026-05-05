#!/usr/bin/env bash
# install_salesforce.sh — One-command installer for the Salesforce → Veza OAA integration
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/<org>/<repo>/main/integrations/salesforce/install_salesforce.sh | bash
#
# Non-interactive (CI/CD):
#   SF_INSTANCE_URL=... SF_TOKEN_URL=... SF_CLIENT_ID=... SF_CLIENT_SECRET=... \
#   VEZA_URL=... VEZA_API_KEY=... bash install_salesforce.sh --non-interactive
#
# Flags:
#   --non-interactive    Skip all prompts; use env vars for credentials
#   --overwrite-env      Overwrite an existing .env file
#   --install-dir <path> Override the installation base directory
#   --repo-url <url>     Override source repository URL
#   --branch <name>      Override repository branch (default: main)
# =============================================================================
set -uo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SCRIPT_NAME="install_salesforce.sh"
SLUG="salesforce"
INTEGRATION_SUBDIR="integrations/salesforce"
REPO_URL="${REPO_URL:-https://github.com/andrewmusto-git/SalesforceUAT.git}"
BRANCH="${BRANCH:-main}"
BASE_DIR="${INSTALL_DIR:-/opt/VEZA/salesforceUAT-veza}"
SCRIPTS_DIR="${BASE_DIR}/scripts"
LOGS_DIR="${BASE_DIR}/logs"
VENV_DIR="${SCRIPTS_DIR}/venv"
ENV_FILE="${SCRIPTS_DIR}/.env"
NON_INTERACTIVE=false
OVERWRITE_ENV=false

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'; BOLD='\033[1m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --non-interactive) NON_INTERACTIVE=true ;;
        --overwrite-env)   OVERWRITE_ENV=true ;;
        --install-dir)     BASE_DIR="$2"; SCRIPTS_DIR="${BASE_DIR}/scripts"; LOGS_DIR="${BASE_DIR}/logs"; VENV_DIR="${SCRIPTS_DIR}/venv"; ENV_FILE="${SCRIPTS_DIR}/.env"; shift ;;
        --repo-url)        REPO_URL="$2"; shift ;;
        --branch)          BRANCH="$2"; shift ;;
        *) warn "Unknown flag: $1" ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
OS_ID=""
PKG_MGR=""
if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    OS_ID=$(grep -E "^ID=" /etc/os-release | cut -d= -f2 | tr -d '"')
fi

if command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
elif command -v apt-get &>/dev/null; then
    PKG_MGR="apt-get"
else
    die "No supported package manager found (dnf/yum/apt-get). Install packages manually."
fi
info "Detected OS: ${OS_ID:-unknown}, package manager: ${PKG_MGR}"

# ---------------------------------------------------------------------------
# Package installer helper (one at a time with pre-check)
# ---------------------------------------------------------------------------
_install_pkg() {
    local pkg="$1"
    info "Installing ${pkg}..."
    case "${PKG_MGR}" in
        dnf|yum) "${PKG_MGR}" install -y "${pkg}" >/dev/null 2>&1 || warn "Failed to install ${pkg} — continuing" ;;
        apt-get) apt-get install -y "${pkg}" >/dev/null 2>&1 || warn "Failed to install ${pkg} — continuing" ;;
    esac
}

# ---------------------------------------------------------------------------
# Step 1 — System prerequisites
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}Step 1 — System Prerequisites${NC}"

command -v git     &>/dev/null || _install_pkg git
command -v python3 &>/dev/null || _install_pkg python3
python3 -m pip --version &>/dev/null || _install_pkg python3-pip

# curl — skip on Amazon Linux if curl-minimal is already present
if ! command -v curl &>/dev/null; then
    if [[ "${OS_ID}" == "amzn" ]]; then
        warn "Skipping curl install on Amazon Linux (curl-minimal conflict)"
    else
        _install_pkg curl
    fi
fi

# python3-venv — check first because it is built-in on AL2023 / RHEL 9+
if ! python3 -m venv --help &>/dev/null; then
    case "${PKG_MGR}" in
        dnf|yum) _install_pkg python3-virtualenv ;;
        apt-get) _install_pkg python3-venv ;;
    esac
fi

# Verify Python version ≥ 3.8
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "${PY_VER}" | cut -d. -f1)
PY_MINOR=$(echo "${PY_VER}" | cut -d. -f2)
if [[ "${PY_MAJOR}" -lt 3 ]] || [[ "${PY_MAJOR}" -eq 3 && "${PY_MINOR}" -lt 8 ]]; then
    die "Python 3.8+ is required. Found: ${PY_VER}"
fi
success "Python ${PY_VER} — OK"

# ---------------------------------------------------------------------------
# Step 2 — Directory layout
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}Step 2 — Directory Layout${NC}"
mkdir -p "${SCRIPTS_DIR}" "${LOGS_DIR}"
success "Directories created: ${BASE_DIR}"

# ---------------------------------------------------------------------------
# Step 3 — Clone repository and copy integration files
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}Step 3 — Fetching Integration Files${NC}"
tmp_dir=$(mktemp -d)
trap 'rm -rf "${tmp_dir}"' EXIT

GIT_TERMINAL_PROMPT=0 git clone \
    --branch "${BRANCH}" \
    --depth 1 \
    --single-branch \
    "${REPO_URL}" "${tmp_dir}" 2>/dev/null || die "git clone failed. Check REPO_URL and BRANCH."

src="${tmp_dir}/${INTEGRATION_SUBDIR}"
[[ -d "${src}" ]] || die "Integration directory not found in repo: ${INTEGRATION_SUBDIR}"

cp -f "${src}/salesforce.py"      "${SCRIPTS_DIR}/"
cp -f "${src}/requirements.txt"   "${SCRIPTS_DIR}/"
[[ -f "${src}/.env.example" ]] && cp -f "${src}/.env.example" "${SCRIPTS_DIR}/.env.example"
success "Integration files copied to ${SCRIPTS_DIR}"

# ---------------------------------------------------------------------------
# Step 4 — Python virtual environment
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}Step 4 — Python Virtual Environment${NC}"
if [[ ! -d "${VENV_DIR}" ]]; then
    python3 -m venv "${VENV_DIR}" || die "Failed to create virtual environment"
    success "Virtual environment created: ${VENV_DIR}"
fi

"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${SCRIPTS_DIR}/requirements.txt" \
    || die "pip install failed. Check ${SCRIPTS_DIR}/requirements.txt"
success "Python dependencies installed"

# ---------------------------------------------------------------------------
# Step 5 — .env configuration
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}Step 5 — Environment Configuration${NC}"

if [[ -f "${ENV_FILE}" && "${OVERWRITE_ENV}" == "false" ]]; then
    warn ".env already exists at ${ENV_FILE} — skipping (use --overwrite-env to replace)"
else
    if [[ "${NON_INTERACTIVE}" == "true" ]]; then
        # Non-interactive: read from environment variables
        SF_INSTANCE_URL="${SF_INSTANCE_URL:-}"
        SF_TOKEN_URL="${SF_TOKEN_URL:-}"
        SF_CLIENT_ID="${SF_CLIENT_ID:-}"
        SF_CLIENT_SECRET="${SF_CLIENT_SECRET:-}"
        VEZA_URL="${VEZA_URL:-}"
        VEZA_API_KEY="${VEZA_API_KEY:-}"
    else
        # Interactive: prompt via /dev/tty
        echo "Enter your Salesforce and Veza credentials."
        echo "(All values are written to ${ENV_FILE} with chmod 600)"
        echo ""

        IFS= read -r -p "Salesforce instance URL [https://your-org.my.salesforce.com]: " SF_INSTANCE_URL </dev/tty
        IFS= read -r -p "Salesforce OAuth2 token URL [https://your-org.my.salesforce.com/services/oauth2/token]: " SF_TOKEN_URL </dev/tty
        IFS= read -r -p "Salesforce Client ID (Consumer Key): " SF_CLIENT_ID </dev/tty
        IFS= read -r -s -p "Salesforce Client Secret (Consumer Secret): " SF_CLIENT_SECRET </dev/tty; echo >/dev/tty
        IFS= read -r -p "Veza URL [https://your-company.veza.com]: " VEZA_URL </dev/tty
        IFS= read -r -s -p "Veza API Key: " VEZA_API_KEY </dev/tty; echo >/dev/tty
    fi

    cat > "${ENV_FILE}" <<EOF
# Salesforce → Veza OAA Integration — Runtime Configuration
# Generated by ${SCRIPT_NAME} on $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# chmod 600 ${ENV_FILE}

# Salesforce
SF_INSTANCE_URL=${SF_INSTANCE_URL}
SF_TOKEN_URL=${SF_TOKEN_URL}
SF_CLIENT_ID=${SF_CLIENT_ID}
SF_CLIENT_SECRET=${SF_CLIENT_SECRET}
# SF_API_VERSION=60.0

# Veza
VEZA_URL=${VEZA_URL}
VEZA_API_KEY=${VEZA_API_KEY}
EOF
    chmod 600 "${ENV_FILE}"
    success ".env created at ${ENV_FILE} (mode 600)"
fi

# ---------------------------------------------------------------------------
# Step 6 — File permissions
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}Step 6 — File Permissions${NC}"
chmod 755 "${SCRIPTS_DIR}"
[[ -f "${ENV_FILE}" ]] && chmod 600 "${ENV_FILE}"
success "Permissions set"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}============================================================${NC}"
echo -e "${GREEN}  Salesforce → Veza OAA Integration — Installation Complete${NC}"
echo -e "${BOLD}============================================================${NC}"
echo ""
echo -e "  Install path  : ${SCRIPTS_DIR}"
echo -e "  Python venv   : ${VENV_DIR}"
echo -e "  Config file   : ${ENV_FILE}"
echo -e "  Log directory : ${LOGS_DIR}"
echo ""
echo -e "${BOLD}Next Steps:${NC}"
echo ""
echo -e "  1. Review and edit ${ENV_FILE}"
echo -e "     (SF_INSTANCE_URL, SF_TOKEN_URL, SF_CLIENT_ID, SF_CLIENT_SECRET, VEZA_URL, VEZA_API_KEY)"
echo ""
echo -e "  2. Run a dry-run to validate the payload:"
echo -e "     cd ${SCRIPTS_DIR}"
echo -e "     ${VENV_DIR}/bin/python3 salesforce.py --env-file .env --dry-run --save-json"
echo ""
echo -e "  3. Push to Veza:"
echo -e "     ${VENV_DIR}/bin/python3 salesforce.py --env-file .env"
echo ""
echo -e "  4. Schedule with cron (example — daily at 02:00):"
echo -e "     0 2 * * * ${VENV_DIR}/bin/python3 ${SCRIPTS_DIR}/salesforce.py --env-file ${ENV_FILE} >> ${LOGS_DIR}/cron.log 2>&1"
echo ""
