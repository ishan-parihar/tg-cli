#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# ─── tg-cli installer ───────────────────────────────────────────────
# curl -fsSL https://raw.githubusercontent.com/ishan-parihar/tg-cli/main/install.sh | bash
#
# Installs tg-cli globally using uv (preferred) or pipx/pip as fallback.
# Handles clean system setup including uv installation and dependency management.
# ──────────────────────────────────────────────────────────────────────

NAME="tg-lyr"
REPO="https://github.com/ishan-parihar/tg-lyr.git"
REPO_GIT="git+${REPO}"
BIN="tg"
MIN_PYTHON_VERSION="3.10"
BIN_DIR="$HOME/.local/bin"

# Colors (TTY-gated)
if [ -t 2 ]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
fi
info()  { echo -e "${GREEN}▸${NC} $*" >&2; }
warn()  { echo -e "${YELLOW}▸${NC} $*" >&2; }
err()   { echo -e "${RED}▸${NC} $*" >&2; }
step()  { echo -e "${BLUE}▸${NC} $*" >&2; }
die()   { err "$*"; exit 1; }
generated() { printf '%s\n' ">>> generated: $1 <<<"; }

# ── Args ───────────────────────────────────────────────────────────────────
# ── Args ───────────────────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --no-cta) SKIP_CTA=1 ;;
    --no-harness) SKIP_HARNESS=1 ;;
    --all-harnesses) ALL_HARNESS=1 ;;
    --help|-h) sed -n '5,10p' "$0"; exit 0 ;;
    --force-root) FORCE_ROOT=1 ;;
    *) die "Unknown option: $arg (supported: --no-cta, --no-harness, --all-harnesses)" ;;
  esac
done

# ── OS / arch detection ───────────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
  Linux*)     DISTRO="$(. /etc/os-release 2>/dev/null && echo "${ID:-unknown}")" ;;
  Darwin*)    DISTRO="macos" ;;
  *) die "Unsupported OS: $OS — Windows: use install.ps1" ;;
esac

# ── Root guard ────────────────────────────────────────────────────────────
if [ "${EUID:-$(id -u)}" -eq 0 ] && [ "${FORCE_ROOT:-0}" != "1" ]; then
  die "Don't run as root — installs to \$HOME. Re-run as your user (override: --force-root)."
fi

# ── Package-manager matrix ────────────────────────────────────────────────
pm_install() {
  case "$DISTRO" in
    ubuntu|debian|linuxmint|pop)      sudo apt-get update -qq && sudo apt-get install -y "$@" ;;
    fedora|rhel|centos|rocky|alma)    sudo dnf install -y "$@" ;;
    arch|manjaro|cachyos|endeavouros)  sudo pacman -Sy --needed --noconfirm "$@" ;;
    opensuse*)                         sudo zypper -n install "$@" ;;
    alpine)                            sudo apk add "$@" ;;
    macos) command -v brew >/dev/null || die "Homebrew required: https://brew.sh (install it, re-run)"; brew install "$@" ;;
    *) die "Unknown distro '$DISTRO' — install manually: $*" ;;
  esac
}
need() {
  command -v "$1" >/dev/null 2>&1 && return 0
  info "Installing prerequisite: $1"
  pm_install "${@:2}"
  command -v "$1" >/dev/null || die "$1 still missing after install"
}

# ── Idempotent PATH injection ─────────────────────────────────────────────
ensure_path() {
  mkdir -p "$BIN_DIR"
  case ":$PATH:" in *":$BIN_DIR:"*) return 0 ;; esac
  local rc="$HOME/.bashrc"
  if [ -f "$HOME/.zshrc" ] || [ -n "${ZSH_VERSION:-}" ]; then rc="$HOME/.zshrc"; fi
  if ! grep -qs "export PATH=\"${BIN_DIR}:\$PATH\"" "$rc"; then
    printf '\n# added by %s installer\nexport PATH="%s:$PATH"\n' "$NAME" "$BIN_DIR" >> "$rc"
    generated "$rc"
  fi
  export PATH="$BIN_DIR:$PATH"
}

# ── Check Python ≥ 3.10 ──────────────────────────────────────────────────
check_python() {
    local py=""
    for cmd in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
        if command -v "$cmd" &>/dev/null; then
            if "$cmd" -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
                py="$cmd"
                break
            fi
        fi
    done

    if [[ -z "$py" ]]; then
        err "Python ${MIN_PYTHON_VERSION}+ required but not found. Install Python ${MIN_PYTHON_VERSION}+ and retry."
        return 1
    fi

    echo "$py"
}

# ── Install uv if not present ─────────────────────────────────────────────
install_uv() {
    if command -v uv &>/dev/null; then
        info "uv already installed: $(uv --version)"
        return 0
    fi

    step "Installing uv..."
    if curl -LsSf https://astral.sh/uv/install.sh | sh; then
        export PATH="$HOME/.local/bin:$PATH"
        if command -v uv &>/dev/null; then
            info "uv installed: $(uv --version)"
            return 0
        fi
    fi

    warn "uv installation failed, falling back to pip-based installation"
    return 1
}

# ── Install via uv (preferred method) ──────────────────────────────────────
install_with_uv() {
    if ! command -v uv &>/dev/null; then
        return 1
    fi

    step "Installing $BIN using uv..."

    if uv tool install "$REPO_GIT" --python "$1"; then
        info "$BIN installed via uv tool"
        ensure_path "$BIN_DIR"
        return 0
    fi

    if uv pip install "$REPO_GIT" --python "$1"; then
        local bin_path
        bin_path=$(find "$BIN_DIR" -name "$BIN" -type f 2>/dev/null | head -1)
        if [[ -z "$bin_path" ]]; then
            bin_path=$(which "$BIN" 2>/dev/null || true)
        fi

        if [[ -n "$bin_path" && -f "$bin_path" ]]; then
            info "$BIN installed via uv pip: $bin_path"
            ensure_path "$BIN_DIR"
            return 0
        else
            warn "Could not locate installed binary. Check: $1 -m pip show tg-lyr"
            return 0
        fi
    fi

    return 1
}

# ── Try pipx first (cleanest global install) ──────────────────────────────
install_pipx() {
    local py="$1"

    if ! command -v pipx &>/dev/null; then
        if command -v uv &>/dev/null; then
            uv tool install pipx --python "$py" 2>/dev/null || true
        else
            "$py" -m pip install --user pipx 2>/dev/null || true
        fi
    fi

    if ! command -v pipx &>/dev/null; then
        return 1
    fi

    step "Installing $BIN using pipx..."

    if pipx install "$REPO_GIT" --python "$py" 2>/dev/null; then
        info "$BIN installed via pipx"
        pipx ensurepath 2>/dev/null || true
        ensure_path "$BIN_DIR"
        return 0
    fi

    if pipx upgrade "$BIN" --python "$py" 2>/dev/null; then
        info "$BIN upgraded via pipx"
        ensure_path "$BIN_DIR"
        return 0
    fi

    return 1
}

# ── Fallback: pip install --user + symlink ────────────────────────────────
install_pip() {
    local py="$1"

    step "Installing $BIN using pip..."

    "$py" -m pip install --user --upgrade pip setuptools wheel >/dev/null 2>&1 || true

    if "$py" -m pip install --user "$REPO_GIT"; then
        info "$BIN installed via pip"

        local user_bin
        user_bin="$("$py" -m site --user-base 2>/dev/null)/bin"
        [[ -d "$user_bin" ]] || user_bin="$BIN_DIR"

        local bin_path
        bin_path=$(find "$user_bin" -name "$BIN" -type f 2>/dev/null | head -1)
        if [[ -z "$bin_path" ]]; then
            bin_path=$(which "$BIN" 2>/dev/null || true)
        fi

        if [[ -n "$bin_path" && -f "$bin_path" ]]; then
            info "Binary found at: $bin_path"
            ensure_path "$BIN_DIR"
            return 0
        else
            warn "Could not locate installed binary. Check: $py -m pip show tg-lyr"
            return 0
        fi
    fi

    return 1
}

# ── Harness detection + SKILL.md registration ───────────────────────────
detect_harnesses() {
    local found=""
    for h in claude codex opencode gemini qwen grok goose hermes zeroclaw omp omo pi droid; do
        command -v "$h" >/dev/null 2>&1 && found="$found $h"
    done
    echo "$found"
}

run_harness_integration() {
    [ "${SKIP_HARNESS:-0}" = "1" ] && { info "Harness integration: skipped (SKIP_HARNESS)"; return 0; }
    if [ -f "SKILL.md" ]; then
        local skill_dir="$HOME/.agents/skills/tg-cli"
        mkdir -p "$skill_dir"
        cp SKILL.md "$skill_dir/SKILL.md"
        generated "$skill_dir/SKILL.md"
        info "Skill installed → ${skill_dir}/SKILL.md"
    fi
    local found; found="$(detect_harnesses)"
    [ -z "$found" ] && { info "No AI harnesses detected — skipping npx skills add"; return 0; }
    if ! [ -t 0 ] && [ "${ALL_HARNESS:-0}" != "1" ]; then
        info "Detected harnesses:${found} — non-interactive, skipping (re-run with --all-harnesses)"
        return 0
    fi
    if [ "${ALL_HARNESS:-0}" = "1" ]; then local answer="y";
    else read -rp "Configure ${found} ? [Y/n] " answer || answer="y"; fi
    case "$answer" in n|N) return 0 ;; esac
    if command -v npx >/dev/null 2>&1; then
        npx --yes skills add ishan-parihar/tg-cli --agent '*' --skill '*' -y 2>/dev/null ||
            warn "npx skills add failed — skill already registered manually"
    fi
    info "Harness integration complete"
}

# ── Verify ────────────────────────────────────────────────────────────────
verify() {
    step "Verifying installation..."
    if command -v "$BIN" &>/dev/null; then
        info "$BIN installed successfully: $($BIN --version 2>/dev/null || echo 'version check failed')"
    else
        warn "Binary not found on PATH. You may need to restart your shell."
        info "Or run: \$HOME/.local/bin/$BIN --help"
        return 1
    fi
}

# ── CTA ───────────────────────────────────────────────────────────────────
print_cta() {
    [ "${SKIP_CTA:-0}" = "1" ] && return 0
    printf '\n%s\n' "──────────────────────────────────────────────────────"
    printf ' ⭐  Found %s useful? Star it:  https://github.com/ishan-parihar/%s\n' "$NAME" "$NAME"
    printf ' 💜  Support development:\n'
    printf '     GitHub Sponsors : https://github.com/sponsors/ishan-parihar\n'
    printf '     Razorpay (UPI)  : https://rzp.io/rzp/ishan-parihar\n'
    printf '     Or run `%s donate` anytime.\n' "$BIN"
    printf '%s\n' "──────────────────────────────────────────────────────"
}

# ── Main ──────────────────────────────────────────────────────────────────
main() {
    info "Installing tg-cli..."
    echo ""

    # Install system dependencies
    need curl curl
    need git git

    # Check Python
    local py
    py=$(check_python) || exit 1
    info "Found Python: $py ($("$py" --version 2>&1))"

    # Try uv first (preferred)
    if install_uv; then
        if install_with_uv "$py"; then
            verify
            run_harness_integration
            finish
            exit 0
        fi
        warn "uv installation failed, trying pipx..."
    fi

    # Try pipx
    if install_pipx "$py"; then
        info "pipx install complete"
        verify
        run_harness_integration
        finish
        exit 0
    fi

    # Fallback to pip
    if install_pip "$py"; then
        info "pip install complete"
        verify
        run_harness_integration
        finish
        exit 0
    fi

    err "All installation methods failed."
    exit 1
}

finish() {
    echo
    info "Next steps:"
    echo "  1. tg refresh             # sync all chats to local DB"
    echo "  2. tg status             # verify auth"
    echo "  3. tg search \"keyword\"    # search messages"
    echo "  4. tg setup --all        # install AI agent session hooks"
    echo
    print_cta
    info "Update anytime:  tg update --check && tg update"
}

main "$@"
