#!/usr/bin/env bash
# ihaveacreditcard bootstrap — linux only (that's the point).
#
# does everything once for you: distro packages, the python venv with the
# custom-install package, building save3ds, building the vendored decryptor
# (decryptor-tui/ctrdecrypt + makerom/ctrtool) and dropping the binaries on
# PATH, plus a nudge about the keys you still have to dump yourself.
#
# usage:
#   ./install.sh              full setup, then offers to start the tui
#   ./install.sh --dry-run    print what it would do, touch nothing
#   ./install.sh --no-sudo    don't call sudo (you'll need packages installed already)
#   ./install.sh --skip-tui   don't offer the tui at the end
#   ./install.sh --no-decryptor  skip the decryptor/decryptor-tui/makerom builds
# extras you can set: CC_SRC_DIR=/somewhere (where the save3ds source is cloned)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${HOME}/.venvs/custom-install"
BIN_DIR="${HOME}/.local/bin"
SRC_DIR="${CC_SRC_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/ihaveacreditcard}"
PART_URL="https://github.com/ihaveamac/custom-install/archive/refs/heads/python-package.zip"
SAVE3DS_URL="https://github.com/wwylele/save3ds.git"
KEYS_GUIDE="https://wiki.hacks.guide/wiki/3DS:Dump_system_files"

DRY_RUN=0 NO_SUDO=0 SKIP_TUI=0 NO_DECRYPTOR=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-sudo) NO_SUDO=1 ;;
    --skip-tui|--no-tui) SKIP_TUI=1 ;;
    --no-decryptor) NO_DECRYPTOR=1 ;;
    *) echo "unknown option: $arg (try --dry-run / --no-sudo / --skip-tui / --no-decryptor)" >&2; exit 2 ;;
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
  if [ -n "$PM" ]; then
    case " apt-get dnf pacman zypper apk xbps-install emerge nixos " in
      *" $PM "*)
        info "package manager forced via PM_FORCE: $PM"
        return
        ;;
      *)
        die "PM_FORCE='$PM' is not supported — known: apt-get dnf pacman zypper apk xbps-install emerge nixos"
        ;;
    esac
  fi
  if [ -f /etc/os-release ] && . /etc/os-release 2>/dev/null && [ "${ID:-}" = "nixos" ]; then
    PM="nixos"
  else
    for p in apt-get dnf pacman zypper apk xbps-install emerge; do
      if have "$p"; then PM="$p"; break; fi
    done
  fi
  [ -n "$PM" ] || die "no supported package manager found (apt/dnf/pacman/zypper/apk/xbps/emerge) —
install by hand: python3 + venv/pip, gcc/make/pkg-config, curl, git, cargo, fuse dev headers"
  info "using package manager: $PM"
}

install_pkgs() {
  info "installing distro packages (sudo may prompt once)"
  # note: if one package name is wrong on a distro, the whole transaction
  # aborts — so the fallbacks below repeat the FULL list, swapping only the
  # fuse package (libfuse3-dev/fuse3-devel have older aliases)
  case "$PM" in
    apt-get)
      sudo_run apt-get update -y
      sudo_run apt-get install -y python3 python3-venv python3-pip build-essential \
        pkg-config curl git cargo rustc libfuse3-dev \
        || sudo_run apt-get install -y python3 python3-venv python3-pip build-essential \
          pkg-config curl git cargo rustc libfuse-dev
      ;;
    dnf)
      sudo_run dnf install -y python3 python3-pip gcc make pkgconf-pkg-config \
        curl git cargo rustc fuse3-devel \
        || sudo_run dnf install -y python3 python3-pip gcc make pkgconf-pkg-config \
          curl git cargo rustc fuse-devel
      ;;
    pacman)
      sudo_run pacman -Sy --needed --noconfirm python python-pip base-devel \
        fuse3 rust curl git || true
      ;;
    zypper)
      sudo_run zypper --non-interactive install python3 python3-pip gcc make \
        pkg-config curl git cargo rust fuse3-devel \
        || sudo_run zypper --non-interactive install python3 python3-pip gcc make \
          pkg-config curl git cargo rust fuse-devel
      ;;
    apk)
      sudo_run apk add --no-cache python3 py3-pip build-base pkgconf fuse-dev \
        curl git cargo rust
      ;;
    xbps-install)
      sudo_run xbps-install -Sy python3 python3-pip base-devel fuse-devel rust curl git
      ;;
    emerge)
      sudo_run emerge --noreplace dev-lang/python dev-python/pip dev-vcs/git \
        net-misc/curl dev-lang/rust sys-fs/fuse-common sys-fs/fuse || true
      ;;
    nixos)
      warn "nixos gets manual hints instead of an install:"
      warn "  nix profile install nixpkgs#curl nixpkgs#git nixpkgs#rustc nixpkgs#cargo"
      warn "python3 is usually already present; rerun this script once those exist."
      warn "(the rest of the toolkit uses zero systemd anywhere, so runit/s66/openrc etc. are fine)"
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
    run install -Dm755 "$SRC_DIR/save3ds/target/release/save3ds_fuse" "$BIN_DIR/save3ds_fuse" \
      || warn "built ok but install failed — run it yourself: install -Dm755 \\
      $SRC_DIR/save3ds/target/release/save3ds_fuse $BIN_DIR/save3ds_fuse"
    return
  fi
  warn "full build failed (probably no fuse dev headers) — retrying without fuse mounting"
  if run bash -c "cd '$SRC_DIR/save3ds' && cargo build --release -p save3ds_fuse --no-default-features" \
     || run bash -c "cd '$SRC_DIR/save3ds/save3ds_fuse' && cargo build --release --no-default-features"; then
    # the artifact lands in the workspace root target dir (member-dir builds
    # can put it in the member's own target) — try both, don't die on failure
    run install -Dm755 "$SRC_DIR/save3ds/target/release/save3ds_fuse" "$BIN_DIR/save3ds_fuse" \
      || run install -Dm755 "$SRC_DIR/save3ds/save3ds_fuse/target/release/save3ds_fuse" \
        "$BIN_DIR/save3ds_fuse" \
      || warn "built ok but install failed — copy save3ds_fuse onto PATH yourself"
  else
    warn "couldn't build save3ds without fuse either — the card listing in the tui needs it."
    warn "build it by hand later: https://github.com/wwylele/save3ds#building"
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
  for f in "$HOME/3ds/movable.sed" "$HOME/.3ds/movable.sed"; do
    if [ -f "$f" ]; then info "  movable: $(tilde2 "$f")"; found=1; break; fi
  done
  [ "$found" = 1 ] || warn "  movable.sed missing from ~/3ds or ~/.3ds — dump it: $KEYS_GUIDE"
}

# ---------------------------------------------------------------- decryptor
# the vendored rust decryptor (decryptor/README.md): ctrdecrypt = library +
# cli, decryptor-tui = the batch decryptor UI. makerom/ctrtool come from
# Project_CTR and are what decryptor-tui calls for the final repack step.
# ---------------------------------------------------------------- decryptor
# the vendored rust decryptor (decryptor/README.md): ctrdecrypt = library +
# cli, decryptor-tui = the batch decryptor UI. makerom/ctrtool come from
# Project_CTR and are what decryptor-tui calls for the final repack step.
setup_decryptor() {
  if [ -x "$HOME/.cargo/bin/decryptor-tui" ] || have decryptor-tui; then
    info "decryptor-tui already available"
    return
  fi
  if ! have cargo; then
    warn "cargo not found → skipping the decryptor build."
    warn "the decrypt flow in the tui needs decryptor-tui: install rust, then rerun."
    return
  fi
  for crate in ctrdecrypt tui; do
    if [ "$crate" = "ctrdecrypt" ]; then bin="ctrdecrypt"; else bin="decryptor-tui"; fi
    info "building vendored decryptor/$crate (cargo release)"
    if run bash -c "cd '$REPO_DIR/decryptor/$crate' && cargo build --release" \
       && [ -x "$REPO_DIR/decryptor/$crate/target/release/$bin" ]; then
      run install -Dm755 "$REPO_DIR/decryptor/$crate/target/release/$bin" "$BIN_DIR/$bin" \
        || warn "built ok but install failed — copy $bin onto PATH yourself"
    else
      warn "couldn't build $crate — see decryptor/README.md for manual steps"
    fi
  done
}

setup_project_ctr() {
  local tool=""
  for tool in ctrtool makerom; do
    if [ -x "$BIN_DIR/$tool" ] || have "$tool"; then
      info "$tool already available"
      continue
    fi
    if [ ! -d "$SRC_DIR/Project_CTR" ]; then
      run git clone --depth 1 https://github.com/3DSGuy/Project_CTR "$SRC_DIR/Project_CTR" \
        || { warn "couldn't clone Project_CTR — makerom build skipped"; return; }
    fi
    info "building $tool (native linux, replaces the windows .exe's)"
    if run bash -c "cd '$SRC_DIR/Project_CTR/$tool' && make deps >/dev/null && make -j\"\$(nproc)\""; then
      run install -Dm755 "$SRC_DIR/Project_CTR/$tool/bin/$tool" "$BIN_DIR/$tool" \
        || warn "built ok but install failed — copy $tool onto PATH yourself"
    else
      warn "couldn't build $tool — decryptor's repack step needs makerom (ctrtool is optional)"
    fi
  done
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
  if [ "$NO_DECRYPTOR" = 0 ]; then
    setup_decryptor
    setup_project_ctr
  fi
  check_keys
  info "running the setup self-check (scripts/doctor.py)"
  run python3 "$REPO_DIR/scripts/tui.py" --check \
    || warn "the check above found blockers — fix them for a smooth run"
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
