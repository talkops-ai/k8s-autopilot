#!/usr/bin/env bash
# Install k8s-autopilot — AI Operations Framework for Kubernetes.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/talkops-ai/k8s-autopilot/main/scripts/install.sh | bash
#
# What it does:
#   1. Checks prerequisites (Docker, Docker Compose, curl/wget, ports, kubeconfig)
#   2. Downloads docker-compose.yml to ~/.k8s-autopilot/
#   3. Pulls the latest images and starts the stack
#   4. Prints the UI URL and next steps
#
# Re-running this script updates images and restarts the stack.
# Your configuration (set via the UI) is persisted across restarts.
#
# Options:
#   --help, -h     Show this help message and exit
#   --stop         Stop a running k8s-autopilot stack
#   --restart      Restart the k8s-autopilot stack
#   --status       Show the status of the k8s-autopilot stack
#   --update       Pull latest images and restart
#   --uninstall    Stop containers and remove ~/.k8s-autopilot
#
# Environment variables:
#   K8S_AUTOPILOT_YES      — set to 1 to skip prompts (for CI/automation)
#   K8S_AUTOPILOT_BRANCH   — branch to download compose file from (default: main)
#   K8S_AUTOPILOT_HOME     — override install directory (default: ~/.k8s-autopilot)

set -euo pipefail

# ── Constants ────────────────────────────────────────────────

REPO_URL="https://raw.githubusercontent.com/talkops-ai/k8s-autopilot"
BRANCH="${K8S_AUTOPILOT_BRANCH:-main}"
INSTALL_DIR="${K8S_AUTOPILOT_HOME:-${HOME}/.k8s-autopilot}"
COMPOSE_FILE="${K8S_AUTOPILOT_COMPOSE_FILE:-${INSTALL_DIR}/docker-compose.yml}"
AGENT_PORT=10102
UI_PORT=8888

# ── Colors & Logging ────────────────────────────────────────

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
  BOLD=$'\033[1m'
  DIM=$'\033[2m'
  RED=$'\033[0;31m'
  GREEN=$'\033[0;32m'
  YELLOW=$'\033[0;33m'
  BLUE=$'\033[0;34m'
  CYAN=$'\033[0;36m'
  NC=$'\033[0m'
else
  BOLD="" DIM="" RED="" GREEN="" YELLOW="" BLUE="" CYAN="" NC=""
fi

log_step()    { printf "${BLUE}==>${NC} ${BOLD}%s${NC}\n" "$*"; }
log_success() { printf "${GREEN}✔${NC}  %s\n" "$*"; }
log_warn()    { printf "${YELLOW}⚠${NC}  %s\n" "$*" >&2; }
log_error()   { printf "${RED}✘${NC}  %s\n" "$*" >&2; }
log_info()    { printf "   %s\n" "$*"; }

# ── Cleanup ──────────────────────────────────────────────────

TEMP_FILES=()
register_temp() { TEMP_FILES+=("$1"); }

cleanup_on_exit() {
  for f in "${TEMP_FILES[@]:-}"; do
    [ -n "$f" ] && [ -f "$f" ] && rm -f "$f" 2>/dev/null || true
  done
}

trap cleanup_on_exit EXIT
trap 'cleanup_on_exit; exit 130' INT TERM

# ── Help ─────────────────────────────────────────────────────

print_help() {
  cat <<'EOF'
Install k8s-autopilot — AI Operations Framework for Kubernetes.

Usage:
  curl -LsSf https://raw.githubusercontent.com/talkops-ai/k8s-autopilot/main/scripts/install.sh | bash

Options:
  --help, -h       Show this help message and exit
  --stop           Stop the k8s-autopilot stack
  --restart        Restart the k8s-autopilot stack
  --status         Show container status
  --update         Pull latest images and restart
  --uninstall      Stop and remove ~/.k8s-autopilot

Environment variables:
  K8S_AUTOPILOT_YES      Skip prompts (CI/automation)
  K8S_AUTOPILOT_BRANCH   Branch to download from (default: main)
  K8S_AUTOPILOT_HOME     Install directory (default: ~/.k8s-autopilot)

Documentation:
  https://github.com/talkops-ai/k8s-autopilot
EOF
}

# ── Argument Handling ────────────────────────────────────────

ACTION="install"

while [ $# -gt 0 ]; do
  case "$1" in
    --help|-h)
      print_help
      exit 0
      ;;
    --stop)       ACTION="stop"; shift ;;
    --restart)    ACTION="restart"; shift ;;
    --status)     ACTION="status"; shift ;;
    --update)     ACTION="update"; shift ;;
    --uninstall)  ACTION="uninstall"; shift ;;
    *)
      log_error "Unknown option: $1"
      print_help >&2
      exit 1
      ;;
  esac
done

# ── Docker Compose Command Resolution ───────────────────────

COMPOSE_CMD=""

resolve_compose_cmd() {
  # Prefer V2 plugin (docker compose), fall back to V1 standalone (docker-compose)
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
  fi
}

# ── Lifecycle Actions (--stop, --restart, --status, --update, --uninstall) ──

print_running_summary() {
  printf "\n"
  printf "   ${CYAN}🌐 UI:${NC}        ${BOLD}http://localhost:${UI_PORT}${NC}\n"
  printf "   ${CYAN}🔧 Agent API:${NC} ${BOLD}http://localhost:${AGENT_PORT}${NC}\n"
  printf "\n"
}

run_lifecycle_action() {
  if [ ! -f "$COMPOSE_FILE" ]; then
    log_error "k8s-autopilot is not installed. Run the installer first:"
    log_info "curl -LsSf ${REPO_URL}/${BRANCH}/scripts/install.sh | bash"
    exit 1
  fi

  resolve_compose_cmd
  if [ -z "$COMPOSE_CMD" ]; then
    log_error "Docker Compose not found."
    exit 1
  fi

  case "$ACTION" in
    stop)
      log_step "Stopping k8s-autopilot..."
      $COMPOSE_CMD -f "$COMPOSE_FILE" down
      log_success "k8s-autopilot stopped."
      ;;
    restart)
      log_step "Restarting k8s-autopilot..."
      $COMPOSE_CMD -f "$COMPOSE_FILE" down
      $COMPOSE_CMD -f "$COMPOSE_FILE" up -d
      log_success "k8s-autopilot restarted."
      print_running_summary
      ;;
    status)
      log_step "k8s-autopilot status"
      $COMPOSE_CMD -f "$COMPOSE_FILE" ps
      ;;
    update)
      log_step "Updating k8s-autopilot..."
      download_compose_file
      $COMPOSE_CMD -f "$COMPOSE_FILE" pull
      $COMPOSE_CMD -f "$COMPOSE_FILE" down
      $COMPOSE_CMD -f "$COMPOSE_FILE" up -d
      log_success "k8s-autopilot updated to latest."
      print_running_summary
      ;;
    uninstall)
      log_step "Uninstalling k8s-autopilot..."
      $COMPOSE_CMD -f "$COMPOSE_FILE" down -v 2>/dev/null || true
      rm -rf "$INSTALL_DIR"
      log_success "k8s-autopilot removed."
      log_info "Docker images are still cached. To free disk space:"
      log_info "  docker rmi talkopsai/k8s-autopilot:latest talkopsai/talkops:latest"
      ;;
  esac
}

# ── Download Helper ──────────────────────────────────────────

download_file() {
  local url="$1"
  local dest="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf "$url" -o "$dest"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$dest" "$url"
  else
    log_error "Neither curl nor wget is installed."
    exit 1
  fi
}

download_compose_file() {
  local url="${REPO_URL}/${BRANCH}/docker-compose.yml"
  local tmp_file
  tmp_file="$(mktemp /tmp/k8s-autopilot-compose-XXXXXX.yml)"
  register_temp "$tmp_file"

  download_file "$url" "$tmp_file"

  # Validate it's a real compose file (basic check)
  if ! grep -q "services:" "$tmp_file" 2>/dev/null; then
    log_error "Downloaded file does not appear to be a valid docker-compose.yml"
    exit 1
  fi

  mkdir -p "$INSTALL_DIR"
  mv "$tmp_file" "$COMPOSE_FILE"
}

# ── Handle lifecycle actions before install ──────────────────

if [ "$ACTION" != "install" ]; then
  run_lifecycle_action
  exit 0
fi

# ══════════════════════════════════════════════════════════════
# INSTALL FLOW
# ══════════════════════════════════════════════════════════════

printf "\n"
printf "  ${BOLD}k8s-autopilot${NC} — AI Operations Framework for Kubernetes\n"
printf "  ${DIM}https://github.com/talkops-ai/k8s-autopilot${NC}\n"
printf "\n"

# ── Phase 1: Prerequisite Checks ────────────────────────────

log_step "Checking prerequisites..."

PREFLIGHT_FAILED=0

# OS detection
OS="unknown"
case "$(uname -s)" in
  Darwin*)        OS="macos" ;;
  Linux*)         OS="linux" ;;
  CYGWIN*|MINGW*|MSYS*) OS="windows" ;;
esac

if [ "$OS" = "windows" ]; then
  log_warn "Windows detected. Running inside WSL (Windows Subsystem for Linux) is strongly recommended."
fi

# Docker
if command -v docker >/dev/null 2>&1; then
  docker_ver="$(docker --version 2>/dev/null | head -1)"
  log_success "Docker: ${docker_ver}"
else
  log_error "Docker is not installed."
  log_info "Install from: https://docs.docker.com/get-docker/"
  PREFLIGHT_FAILED=1
fi

# Docker daemon running
if [ "$PREFLIGHT_FAILED" -eq 0 ]; then
  if docker info >/dev/null 2>&1; then
    log_success "Docker daemon is running"
  else
    log_error "Docker daemon is not running."
    if [ "$OS" = "macos" ]; then
      log_info "Start Docker Desktop and try again."
    else
      log_info "Start the Docker daemon: sudo systemctl start docker"
    fi
    PREFLIGHT_FAILED=1
  fi
fi

# Docker Compose
resolve_compose_cmd
if [ -n "$COMPOSE_CMD" ]; then
  compose_ver="$($COMPOSE_CMD version 2>/dev/null | head -1)"
  log_success "Docker Compose: ${compose_ver}"
else
  log_error "Docker Compose is not installed."
  log_info "Install from: https://docs.docker.com/compose/install/"
  PREFLIGHT_FAILED=1
fi

# curl or wget
if command -v curl >/dev/null 2>&1; then
  log_success "curl is available"
elif command -v wget >/dev/null 2>&1; then
  log_success "wget is available"
else
  log_error "Neither curl nor wget is installed."
  PREFLIGHT_FAILED=1
fi

# Stop on hard failures
if [ "$PREFLIGHT_FAILED" -eq 1 ]; then
  printf "\n"
  log_error "Prerequisites not met. Please fix the issues above and re-run the installer."
  exit 1
fi

# Port checks (warnings only — don't block)
check_port() {
  local port="$1"
  local label="$2"
  local in_use=0

  if command -v lsof >/dev/null 2>&1; then
    if lsof -i ":${port}" -sTCP:LISTEN >/dev/null 2>&1; then
      in_use=1
    fi
  elif command -v ss >/dev/null 2>&1; then
    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
      in_use=1
    fi
  fi

  if [ "$in_use" -eq 1 ]; then
    log_warn "Port ${port} (${label}) is already in use. The service may fail to start."
  else
    log_success "Port ${port} (${label}) is available"
  fi
}

check_port "$AGENT_PORT" "Agent API"
check_port "$UI_PORT" "UI"

# kubeconfig (warning only)
if [ -f "${HOME}/.kube/config" ]; then
  log_success "kubeconfig found at ~/.kube/config"
else
  log_warn "No kubeconfig found at ~/.kube/config"
  log_info "Cluster features will be unavailable until you mount a kubeconfig."
  log_info "You can configure this later from the Settings UI."
fi

printf "\n"

# ── Phase 2: Download & Start ────────────────────────────────

# Check for existing installation
if [ -f "$COMPOSE_FILE" ]; then
  log_step "Existing installation found at ${INSTALL_DIR}"
  log_info "Updating to the latest version..."

  # Pull latest compose file
  download_compose_file
  log_success "docker-compose.yml updated"

  # Pull latest images
  log_step "Pulling latest images..."
  $COMPOSE_CMD -f "$COMPOSE_FILE" pull

  # Restart
  log_step "Restarting k8s-autopilot..."
  $COMPOSE_CMD -f "$COMPOSE_FILE" down 2>/dev/null || true
  $COMPOSE_CMD -f "$COMPOSE_FILE" up -d

else
  log_step "Setting up k8s-autopilot in ${INSTALL_DIR}"

  # Download compose file
  download_compose_file
  log_success "docker-compose.yml downloaded"

  # Pull images
  log_step "Pulling images (this may take a minute on first run)..."
  $COMPOSE_CMD -f "$COMPOSE_FILE" pull

  # Start
  log_step "Starting k8s-autopilot..."
  $COMPOSE_CMD -f "$COMPOSE_FILE" up -d
fi

# ── Phase 3: Wait for Healthy ────────────────────────────────

log_step "Waiting for services to start..."

WAIT_TIMEOUT=60
WAIT_INTERVAL=3
elapsed=0

while [ "$elapsed" -lt "$WAIT_TIMEOUT" ]; do
  # Check if containers are running
  running=$($COMPOSE_CMD -f "$COMPOSE_FILE" ps --format '{{.State}}' 2>/dev/null | grep -c "running" || echo "0")
  if [ "$running" -ge 2 ]; then
    break
  fi
  sleep "$WAIT_INTERVAL"
  elapsed=$((elapsed + WAIT_INTERVAL))
done

if [ "$elapsed" -ge "$WAIT_TIMEOUT" ]; then
  log_warn "Services are taking longer than expected to start."
  log_info "Check logs with: $COMPOSE_CMD -f $COMPOSE_FILE logs -f"
else
  log_success "k8s-autopilot is running!"
fi

# ── Phase 4: Summary ────────────────────────────────────────

printf "\n"
printf "  ${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
printf "  ${GREEN}✔${NC}  ${BOLD}k8s-autopilot is ready!${NC}\n"
printf "  ${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
printf "\n"
printf "   ${CYAN}🌐 UI:${NC}        ${BOLD}http://localhost:${UI_PORT}${NC}\n"
printf "   ${CYAN}🔧 Agent API:${NC} ${BOLD}http://localhost:${AGENT_PORT}${NC}\n"
printf "\n"
printf "  ${BOLD}Next steps:${NC}\n"
printf "   1. Open ${BOLD}http://localhost:${UI_PORT}${NC} in your browser\n"
printf "   2. Go to ${BOLD}Settings → Auth & Keys${NC} to configure your LLM provider\n"
printf "   3. Browse the ${BOLD}Plugin Marketplace${NC} to extend capabilities\n"
printf "   4. Start chatting — ask anything about your cluster!\n"
printf "\n"
printf "  ${BOLD}Commands:${NC}\n"
printf "   ${DIM}View logs:${NC}   ${CYAN}$COMPOSE_CMD -f ${COMPOSE_FILE} logs -f${NC}\n"
printf "   ${DIM}Stop:${NC}        ${CYAN}$COMPOSE_CMD -f ${COMPOSE_FILE} down${NC}\n"
printf "   ${DIM}Restart:${NC}     ${CYAN}$COMPOSE_CMD -f ${COMPOSE_FILE} up -d${NC}\n"
printf "   ${DIM}Update:${NC}      ${CYAN}curl -LsSf ${REPO_URL}/${BRANCH}/scripts/install.sh | bash${NC}\n"
printf "\n"
