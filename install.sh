#!/usr/bin/env bash
set -euo pipefail

# ─── tg-cli installer ───────────────────────────────────────────────
# curl -sSL https://raw.githubusercontent.com/ishan-parihar/tg-cli/main/install.sh | bash
#
# Installs tg-cli globally using uv (preferred) or pipx/pip as fallback.
# Handles clean system setup including uv installation and dependency management.
# ──────────────────────────────────────────────────────────────────────

REPO="https://github.com/ishan-parihar/tg-cli.git"
REPO_GIT="git+${REPO}"
BIN="tg"
MIN_PYTHON_VERSION="3.10"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${GREEN}▸${NC} $*" >&2; }
warn()  { echo -e "${YELLOW}▸${NC} $*" >&2; }
err()   { echo -e "${RED}▸${NC} $*" >&2; }
step()  { echo -e "${BLUE}▸${NC} $*" >&2; }

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
        err "Python ≥ 3.10 required but not found. Install Python 3.10+ and retry."
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

# ── Install via uv (preferred method) ───────────────────────────────────────
install_with_uv() {
    if ! command -v uv &>/dev/null; then
        return 1
    fi

    step "Installing $BIN using uv..."

    # Install from git repo
    if uv tool install "$REPO_GIT" --python "$1"; then
        info "$BIN installed via uv tool"

        # Ensure ~/.local/bin is in PATH
        ensure_path "$HOME/.local/bin"
        return 0
    fi

    # Fallback: install with uv pip
    if uv pip install "$REPO_GIT" --python "$1"; then
        # Find the binary
        local bin_path
        bin_path=$(find "$HOME/.local/bin" -name "$BIN" -type f 2>/dev/null | head -1)
        if [[ -z "$bin_path" ]]; then
            bin_path=$(which "$BIN" 2>/dev/null || true)
        fi

        if [[ -n "$bin_path" && -f "$bin_path" ]]; then
            info "$BIN installed via uv pip: $bin_path"
            ensure_path "$HOME/.local/bin"
            return 0
        else
            warn "Could not locate installed binary. Check: $1 -m pip show kabi-tg-cli"
            return 0
        fi
    fi

    return 1
}

# ── Try pipx first (cleanest global install) ──────────────────────────────
install_pipx() {
    local py="$1"

    if ! command -v pipx &>/dev/null; then
        # Try to install pipx via uv or pip
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
        return 0
    fi

    # Try upgrade if already installed
    if pipx upgrade "$BIN" --python "$py" 2>/dev/null; then
        info "$BIN upgraded via pipx"
        return 0
    fi

    return 1
}

# ── Fallback: pip install --user + symlink ────────────────────────────────
install_pip() {
    local py="$1"

    step "Installing $BIN using pip..."

    # Ensure pip is up to date
    "$py" -m pip install --user --upgrade pip setuptools wheel >/dev/null 2>&1 || true

    if "$py" -m pip install --user "$REPO_GIT"; then
        info "$BIN installed via pip"

        # Find the binary
        local user_bin
        user_bin="$("$py" -m site --user-base 2>/dev/null)/bin"
        [[ -d "$user_bin" ]] || user_bin="$HOME/.local/bin"

        local bin_path
        bin_path=$(find "$user_bin" -name "$BIN" -type f 2>/dev/null | head -1)
        if [[ -z "$bin_path" ]]; then
            bin_path=$(which "$BIN" 2>/dev/null || true)
        fi

        if [[ -n "$bin_path" && -f "$bin_path" ]]; then
            info "Binary found at: $bin_path"
            ensure_path "$user_bin"
            return 0
        else
            warn "Could not locate installed binary. Check: $py -m pip show kabi-tg-cli"
            return 0
        fi
    fi

    return 1
}

# ── Ensure directory is in PATH ────────────────────────────────────────────
ensure_path() {
    local dir="$1"
    case ":$PATH:" in
        *":$dir:"*) return 0 ;;
    esac

    warn "$dir is not in your PATH."
    warn "Add this to your shell profile (~/.bashrc, ~/.zshrc, etc.):"
    warn "  export PATH=\"\$HOME/.local/bin:\$PATH\""

    # Try to add it automatically for common shells
    local profile=""
    if [[ -n "${ZSH_VERSION:-}" ]]; then
        profile="$HOME/.zshrc"
    elif [[ -n "${BASH_VERSION:-}" ]]; then
        profile="$HOME/.bashrc"
    fi

    if [[ -n "$profile" && -f "$profile" ]]; then
        if ! grep -q '\.local/bin' "$profile" 2>/dev/null; then
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$profile"
            info "Added ~/.local/bin to PATH in $profile"
            info "Run 'source $profile' or restart your shell"
        fi
    fi
}

# ── Install dependencies if needed ─────────────────────────────────────────
install_deps() {
    # Check for curl
    if ! command -v curl &>/dev/null; then
        warn "curl not found. Installing..."
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y curl
        elif command -v yum &>/dev/null; then
            sudo yum install -y curl
        elif command -v pacman &>/dev/null; then
            sudo pacman -S --noconfirm curl
        else
            warn "Could not install curl automatically. Please install it manually."
        fi
    fi

    # Check for git
    if ! command -v git &>/dev/null; then
        warn "git not found. Installing..."
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y git
        elif command -v yum &>/dev/null; then
            sudo yum install -y git
        elif command -v pacman &>/dev/null; then
            sudo pacman -S --noconfirm git
        else
            err "git is required but not found. Please install it manually."
            return 1
        fi
    fi
}

# ── Verify ────────────────────────────────────────────────────────────────
verify() {
    step "Verifying installation..."
    if command -v "$BIN" &>/dev/null; then
        info "$BIN installed successfully: $($BIN --version 2>/dev/null || echo 'version check failed')"
        return 0
    else
        warn "Binary not found on PATH. You may need to restart your shell."
        info "Or run: \$HOME/.local/bin/$BIN --help"
        return 1
    fi
}

# ── Main ──────────────────────────────────────────────────────────────────
main() {
    info "Installing tg-cli..."
    echo ""

    # Install system dependencies
    install_deps

    # Check Python
    local py
    py=$(check_python) || exit 1

    # Try uv first (preferred)
    if install_uv; then
        if install_with_uv "$py"; then
            verify
            return 0
        fi
        warn "uv installation failed, trying pipx..."
    fi

    # Try pipx
    if install_pipx "$py"; then
        info "pipx install complete"
        verify
        return 0
    fi

    # Fallback to pip
    if install_pip "$py"; then
        info "pip install complete"
        verify
        return 0
    fi

    err "All installation methods failed."
    exit 1
}

main "$@"