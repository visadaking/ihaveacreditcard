#!/usr/bin/env bash
# ihaveacreditcard bootstrap — linux only (that's the point).
#
# does everything once for you: distro packages, the python venv with the
# custom-install package, building save3ds and dropping the binaries on PATH,
# plus a nudge about the keys you still have to dump yourself.
#
# usage:
#   ./install.sh            full setup, then offers to start the tui
#   ./install.sh --dry-run  print what it would do, touch nothing
#   ./install.sh --no-sudo  don't call sudo (you'll need packages installed already)
#   ./install.sh --skip-tui don't offer the tui at the end
# extras you can set: CC_SRC_DIR=/somewhere (where the save3ds source is cloned)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${HOME}/.venvs/custom-install"
BIN_DIR="${HOME}/.local/bin"
SRC_DIR="${CC_SRC_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/ihaveacreditcard}"
PART_URL="https://github.com/ihaveamac/custom-install/archive/refs/heads/python-package.zip"
SAVE3DS_URL="https://github.com/wwylele/save3ds.git"
KEYS_GUIDE="https://wiki.hacks.guide/wiki/3DS:Dump_system_files"

DRY_RUN=0 NO_SUDO=0 SKIP_TUI=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-sudo) NO_SUDO=1 ;;
    --skip-tui|--no-tui) SKIP_TUI=1 ;;
    *) echo "unknown option: $arg (try --dry-run / --no-sudo / --skip-tui)" >&2; exit 2 ;;
  esac
done

info() { printf '\033[1;36m==\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m!!\033[0m %s\n' "$*"; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

run() {  # everything that changes things goes through here (so --dry-run works)
  if [ "$DRY_RUN" = 1 ]; then
    printf '\033[2m(dry-run)\033[0m ' ; printf '%q ' "$@" ; printf '\n'
    return 0
  fi
  "$@"
}

sudo_run() {
  if [ "$(id -u)" -eq 0 ]; then
    run "$@"
  elif [ "$NO_SUDO" = 1 ]; then
    warn "skipping privileged step (pass sudo yourself): $*"
  else
    run sudo "$@"
  fi
}

detect_pm() {
  PM="${PM_FORCE:-}"
  if [ -z "$PM" ]; then
    for p in apt-get dnf pacman zypper apk; do
      if have "$p"; then PM="$p"; break; fi
    done
  fi
  [ -n "$PM" ] || die "no supported package manager found (apt/dnf/pacman/zypper/apk) —
install by hand: python3 + venv/pip, gcc/make/pkg-config, curl, git, cargo, fuse dev headers"
  info "using package manager: $PM"
}

install_pkgs() {
  info "installing distro packages (sudo may prompt once)"
  case "$PM" in
    apt-get)
      sudo_run apt-get update -y
      sudo_run apt-get install -y python3 python3-venv python3-pip build-essential \
        pkg-config curl git cargo rustc libfuse3-dev \
        || sudo_run apt-get install -y libfuse-dev
      ;;
    dnf)
      sudo_run dnf install -y python3 python3-pip gcc make pkgconf-pkg-config \
        curl git cargo rustc fuse3-devel || sudo_run dnf install -y fuse-devel
      ;;
    pacman)
      sudo_run pacman -Sy --needed --noconfirm python python-pip base-devel \
        fuse3 rust curl git || true
      ;;
    zypper)
      sudo_run zypper --non-interactive install python3 python3-pip gcc make \
        pkg-config curl git cargo rust fuse3-devel \
        || sudo_run zypper --non-interactive install fuse-devel
      ;;
    apk)
      sudo_run apk add --no-cache python3 py3-pip build-base pkgconf fuse-dev \
        curl git cargo rust
      ;;
  esac
}

check_python() {
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "python >= 3.9 is required (found: $(python3 -V 2>&1 || echo none))"
  info "python: $(python3 -V)"
}

setup_toolchain() {
  info "python toolchain → $VENV_DIR"
  if [ -x "$VENV_DIR/bin/custominstall" ]; then
    run "$VENV_DIR/bin/pip" install --upgrade "$PART_URL"
  else
    run python3 -m venv "$VENV_DIR"
    run "$VENV_DIR/bin/pip" install --upgrade "$PART_URL"
  fi
}

setup_save3ds() {
  if have save3ds_fuse || [ -x "$BIN_DIR/save3ds_fuse" ]; then
    info "save3ds_fuse already available"
    return
  fi
  if ! have cargo; then
    warn "cargo not found → skipping the save3ds build."
    warn "the card listing in the tui needs save3ds_fuse: install rust, then rerun."
    return
  fi
  if [ ! -d "$SRC_DIR/save3ds" ]; then
    run git clone --depth 1 "$SAVE3DS_URL" "$SRC_DIR/save3ds"
  else
    run git -C "$SRC_DIR/save3ds" pull --ff-only 2>/dev/null \
      || warn "couldn't refresh the save3ds source; building the existing copy"
  fi
  info "building save3ds (takes a minute or two)"
  if run bash -c "cd '$SRC_DIR/save3ds' && cargo build --release"; then
    run install -Dm755 "$SRC_DIR/save3ds/target/release/save3ds_fuse" "$BIN_DIR/save3ds_fuse"
  else
    warn "full build failed (probably no fuse dev headers) — retrying without fuse mounting"
    run bash -c "cd '$SRC_DIR/save3ds/save3ds_fuse' && cargo build --release --no-default-features"
    run install -Dm755 "$SRC_DIR/save3ds/save3ds_fuse/target/release/save3ds_fuse" \
      "$BIN_DIR/save3ds_fuse"
  fi
}

check_keys() {
  info "checking keys (you dump these from your own console)"
  local found=0
  for f in "$HOME/3ds/boot9.bin" "$HOME/.3ds/boot9.bin"; do
    if [ -f "$f" ]; then info "  boot9: $(tilde2 "$f")"; found=1; break; fi
  done
  [ "$found" = 1 ] || warn "  boot9.bin missing — dump it: $KEYS_GUIDE"
  found=0
  for f in "$HOME/3ds/movable.sed"; do
    if [ -f "$f" ]; then info "  movable: $(tilde2 "$f")"; found=1; break; fi
  done
  [ "$found" = 1 ] || warn "  movable.sed missing from ~/3ds — dump it: $KEYS_GUIDE"
}

tilde2() { printf '%s' "${1/#$HOME/~}"; }

main() {
  [ "$(id -u)" -eq 0 ] && warn "you're root — stuff will end up in root's home; usually not what you want"
  info "ihaveacreditcard bootstrap (linux only)"
  detect_pm
  install_pkgs
  check_python
  setup_toolchain
  setup_save3ds
  check_keys
  case ":$PATH:" in
    *":$BIN_DIR:"*) info "PATH ok" ;;
    *) warn "$BIN_DIR is not on PATH — add this to your shell rc: export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
  if [ "$DRY_RUN" = 1 ]; then
    info "dry run done (nothing was actually installed)"
    exit 0
  fi
  info "all set — the tui is at $REPO_DIR/scripts/tui.py"
  if [ "$SKIP_TUI" = 0 ] && [ -t 0 ] && [ -t 1 ] &&
     [ -x "$VENV_DIR/bin/custominstall" ]; then
    local ans=""
    printf '\033[1;36m?\033[0m start the tui now? [Y/n] '
    read -r ans || ans="y"
    case "${ans:-y}" in
      n|N|no) ;;
      *) cd "$REPO_DIR" && exec python3 scripts/tui.py ;;
    esac
  fi
}

main "$@"
