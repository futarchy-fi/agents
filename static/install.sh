#!/bin/bash
set -euo pipefail

# Futarchy CLI Installer
# Usage: curl -fsSL https://futarchy.ai/install.sh | bash

BOLD='\033[1m'
DIM='\033[2m'
PURPLE='\033[38;5;141m'
TEAL='\033[38;5;43m'
RED='\033[31m'
NC='\033[0m'

info()    { printf "${DIM}%s${NC}\n" "$*"; }
success() { printf "${TEAL}%s${NC}\n" "$*"; }
error()   { printf "${RED}error:${NC} %s\n" "$*" >&2; }
header()  { printf "\n${PURPLE}${BOLD}%s${NC}\n" "$*"; }

REPO="https://github.com/futarchy-fi/agents.git"
SPEC="futarchy @ git+${REPO}#subdirectory=cli"

header "Futarchy CLI Installer"
echo ""

# --- Detect OS ---
OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM="macOS" ;;
    Linux)  PLATFORM="Linux" ;;
    *)      error "Unsupported OS: $OS"; exit 1 ;;
esac
info "Platform: $PLATFORM"

# --- Check Python ---
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    error "Python 3.10+ is required but not found."
    echo ""
    if [ "$PLATFORM" = "macOS" ]; then
        info "Install with:  brew install python3"
    else
        info "Install with:  sudo apt install python3 python3-pip   (Debian/Ubuntu)"
        info "           or: sudo dnf install python3 python3-pip   (Fedora)"
    fi
    exit 1
fi
info "Python: $($PYTHON --version)"

# --- Choose install method ---
USE_PIPX=false
if command -v pipx &>/dev/null; then
    USE_PIPX=true
    info "Installer: pipx"
elif [ "$PLATFORM" = "macOS" ]; then
    # On macOS, pip install --user often doesn't work (externally managed)
    # Try to install pipx first
    if command -v brew &>/dev/null; then
        info "Installing pipx via Homebrew..."
        brew install pipx 2>/dev/null && pipx ensurepath 2>/dev/null && USE_PIPX=true
    fi
fi

# --- Uninstall old version if present ---
if command -v futarchy &>/dev/null; then
    info "Removing previous installation..."
    if $USE_PIPX; then
        pipx uninstall futarchy 2>/dev/null || true
    else
        $PYTHON -m pip uninstall futarchy -y 2>/dev/null || true
    fi
fi

# --- Install ---
header "Installing..."
echo ""

if $USE_PIPX; then
    pipx install "$SPEC"
else
    $PYTHON -m pip install "$SPEC" --break-system-packages 2>/dev/null \
        || $PYTHON -m pip install "$SPEC" --user 2>/dev/null \
        || $PYTHON -m pip install "$SPEC"
fi

# --- Verify ---
echo ""
if command -v futarchy &>/dev/null; then
    success "futarchy $(futarchy --version 2>/dev/null || echo "0.1.0") installed successfully!"
    echo ""
    info "Get started:"
    printf "  ${BOLD}futarchy markets${NC}        ${DIM}# browse open markets${NC}\n"
    printf "  ${BOLD}futarchy login${NC}          ${DIM}# authenticate${NC}\n"
    printf "  ${BOLD}futarchy buy 1 yes 50${NC}   ${DIM}# trade${NC}\n"
else
    success "futarchy installed!"
    echo ""
    # Might need PATH update
    if $USE_PIPX; then
        info "Run 'pipx ensurepath' and restart your shell, then:"
    else
        info "You may need to restart your shell, then:"
    fi
    printf "  ${BOLD}futarchy markets${NC}\n"
fi
echo ""
