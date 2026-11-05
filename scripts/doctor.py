#!/usr/bin/env python3
"""setup self-check ("the doctor") for the toolkit.

verifies, in plain language, that the pc side is actually usable: python
version, the key files (checked by magic bytes and size, not just existence),
that save3ds_fuse and custominstall really *execute*, and whether the sd card
is mounted writable. missing pieces are warnings — the setup may simply not be
done yet. existing-but-broken pieces are blockers ("fail"), because they break
runs even when everything else looks fine.

usage (all equivalent):

    python3 scripts/tui.py --check
    python3 scripts/doctor.py
    python3 scripts/doctor.py --strict    # warnings also fail, for scripts

byte-level ground truth comes from real dumps: a movable.sed is 320 bytes
starting with ascii 'SEED'; a trimmed boot9.bin is a 64 KiB block whose first
word is an arm branch opcode (0xEA in the top byte); seeddb.bin is a 16-byte
header (u32 count + padding) + count × 32-byte entries.
"""

from __future__ import annotations

import os
import subprocess
import shutil
import sys

import tui  # the same probing logic (dirs, env vars, sd candidates), not a copy

KEYS_GUIDE = "https://wiki.hacks.guide/wiki/3DS:Dump_system_files"
SEEDDB_REPO = "https://github.com/ihaveamac/seeddb"

MOVABLE_SIZE = 0x140   # 320 bytes, b"SEED" magic + version
BOOT9_SIZE = 0x10000   # trimmed arm9 bootrom dump
SEEDDB_HEADER = 16     # u32 entry count + 12 bytes pad (verified on a real file)
SEEDDB_ENTRY = 32      # title id + 16-byte seed + padding

REPORT = {"ok": "ok   ", "warn": "warn ", "fail": "FAIL "}


def check_python() -> tuple[str, str]:
    v = sys.version_info
    if (v.major, v.minor) >= (3, 9):
        return "ok", f"python v{v.major}.{v.minor}.{v.micro} (custom-install wants >= 3.9)"
    return "fail", (f"python v{v.major}.{v.minor} is too old — "
                    "custom-install wants >= 3.9")


def check_boot9(path: str | None) -> tuple[str, str]:
    if not path:
        return "warn", f"boot9.bin not found — dump it: {KEYS_GUIDE}"
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError as err:
        return "fail", f"boot9 can't be read: {tui.tilde(path)} ({err})"
    if size < BOOT9_SIZE:
        return "fail", (f"boot9 at {tui.tilde(path)} is suspiciously small "
                        f"({size} bytes) — likely a truncated dump")
    if size == BOOT9_SIZE:
        if len(head) >= 4 and head[3] == 0xEA:  # arm vector table on a trimmed dump
            return "ok", f"boot9: {tui.tilde(path)} (trimmed bootrom, vector table ok)"
        return "fail", (f"boot9 at {tui.tilde(path)} has the trimmed size but no "
                        "arm vector table at the start — wrong file?")
    return "ok", (f"boot9: {tui.tilde(path)} ({size / 1024 / 1024:.1f} MiB — "
                  "unusual size, tools usually still accept it)")


def check_movable(path: str | None) -> tuple[str, str]:
    if not path:
        return "warn", f"movable.sed not found — dump it: {KEYS_GUIDE}"
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError as err:
        return "fail", f"movable.sed can't be read: {tui.tilde(path)} ({err})"
    if size != MOVABLE_SIZE or head[:4] != b"SEED":
        return "fail", (f"movable.sed at {tui.tilde(path)} looks wrong "
                        f"({size} bytes, starts {head[:4]!r} — a real one is 320 "
                        "bytes starting with 'SEED', not an exported/edited copy)")
    return "ok", f"movable.sed: {tui.tilde(path)} (320 bytes, seed header ok)"


def check_seeddb(path: str | None) -> tuple[str, str]:
    if not path:
        return "warn", ("seeddb.bin not found — only needed for seed-encrypted "
                        f"titles (grab one from {SEEDDB_REPO})")
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError as err:
        return "fail", f"seeddb can't be read: {tui.tilde(path)} ({err})"
    count = int.from_bytes(head[:4], "little")
    if size != SEEDDB_HEADER + count * SEEDDB_ENTRY or count == 0:
        return "fail", (f"seeddb at {tui.tilde(path)} is malformed ({size} bytes; "
                        f"header claims {count} entries → "
                        f"{SEEDDB_HEADER + count * SEEDDB_ENTRY} expected) — "
                        "re-download it from the seeddb repo")
    return "ok", f"seeddb: {tui.tilde(path)} ({count} entries)"


def check_tool(path: str | None, name: str, why: str) -> tuple[str, str]:
    if not path:
        return "warn", f"{name} not found — {why}"
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return "fail", f"{name} is at {tui.tilde(path)} but isn't executable"
    try:
        proc = subprocess.run([path, "--help"], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as err:
        return "fail", f"{name} is on disk but won't run ({err})"
    if proc.returncode not in (0, 1, 2):
        return "fail", f"{name} runs but --help exited {proc.returncode} — broken install?"
    return "ok", f"{name}: {tui.tilde(path)} (runs)"


def writable_problems(mount: str) -> str | None:
    probe = os.path.join(mount, ".ihaveacreditcard_write_test")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(probe)
        return None
    except OSError as err:
        return str(err)


def collect() -> tuple[list[tuple[str, str]], int, int]:
    out = [
        check_python(),
        check_boot9(tui.find_file("boot9.bin", "BOOT9_PATH")),
        check_movable(tui.find_file("movable.sed")),
        check_seeddb(tui.find_file("seeddb.bin", "SEEDDB_PATH")),
        check_tool(tui.find_tool("save3ds_fuse",
                                 (os.environ.get("CUSTOM_INSTALL_SAVE3DS_PATH", ""),)),
                   "save3ds_fuse", "card listing needs it (build wwylele/save3ds)"),
        check_tool(tui.find_tool("custominstall",
                                 ("~/.venvs/custom-install/bin/custominstall",)),
                   "custominstall", "the installer (ihaveamac python-package)"),
    ]

    if shutil.which("save3ds_fuse") is None and \
            os.path.isfile(os.path.expanduser("~/.local/bin/save3ds_fuse")):
        out.append(("warn", "~/.local/bin is not on PATH — "
                            'export PATH="$HOME/.local/bin:$PATH"'))

    media = [c for c in tui.sd_candidates() if c.get("media")]
    if not media:
        out.append(("warn", "no fat/exfat sd card detected — card flows will "
                            "complain until one is plugged in"))
    for cand in media:
        name = tui.tilde(cand["mount"])
        if cand.get("ro"):
            out.append(("fail", f"sd card at {name} is MOUNTED READ-ONLY — "
                                "reseat it (bad contact) or remount rw"))
            continue
        err = writable_problems(cand["mount"])
        if err is None:
            out.append(("ok", f"sd card at {name} ({cand['fs']}) is writable"))
        else:
            out.append(("fail", f"sd card at {name} can't be written ({err})"))

    backup_root = tui.card_backup_root()
    if backup_root:
        out.append(("ok", f"auto backups land in {tui.tilde(backup_root)}"))
    else:
        out.append(("warn", "couldn't create the auto-backup dir in the documents "
                            "folder (set IHC_BACKUP_DIR to point somewhere valid)"))

    warns = sum(1 for level, _ in out if level == "warn")
    fails = sum(1 for level, _ in out if level == "fail")
    return out, warns, fails


def page_lines(findings: list[tuple[str, str]], warns: int, fails: int) -> list[str]:
    lines = [f"{REPORT.get(level, level + '   ')}  {msg}" for level, msg in findings]
    lines.append("")
    if fails:
        lines.append(f"{fails} blocker(s) + {warns} warning(s) → fix the FAIL "
                     "lines above, then rerun this check")
    elif warns:
        lines.append(f"{warns} warning(s), no blockers → usable; warnings are "
                     "just things not set up yet")
    else:
        lines.append("no blockers, no warnings → setup is in good shape")
    return lines


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    findings, warns, fails = collect()
    print("== setup check ==\n")
    for line in page_lines(findings, warns, fails):
        print(f"  {line}")
    if fails or ("--strict" in argv and warns):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
