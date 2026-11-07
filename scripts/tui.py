#!/usr/bin/env python3
"""tui for the toolkit: check the setup, install dumps, decrypt dumps, list card
titles, uninstall titles, write cifinish.bin.

run from inside the repo:

    python3 scripts/tui.py

it drives the exact same tools documented in the README (the custominstall
binary, save3ds_fuse, the vendored decryptor-tui, boot9/movable keys) — no
crypto is reimplemented here.

if the terminal can't do full-screen rendering (or you pass --plain), it falls
back to a plain numbered menu that offers the exact same functions.
"""

from __future__ import annotations

import glob
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if os.fspath(HERE) not in sys.path:
    sys.path.insert(0, os.fspath(HERE))

from inspect_title_db import decode_entry, entry_lines          # noqa: E402
from make_cifinish import build as build_cifinish               # noqa: E402

APP_TITLE = "ihaveacreditcard · 3ds sd install toolkit (v0.2)"
KEY_DIRS = ("~/3ds", "~/.3ds")
SD_MOUNT_PREFIXES = ("/run/media/", "/media/")
FAT_TYPES = {"vfat", "exfat", "msdos", "fat", "ufsd", "tf_fat"}
DUMP_EXTS = (".3ds", ".3dz", ".cia")
PACKAGE_URL = ("https://github.com/ihaveamac/custom-install/archive/"
               "refs/heads/python-package.zip")

# auto backups: small, hard-to-regenerate card files land here before the
# toolkit touches anything risky
BACKUP_DIRNAME = "ihaveacreditcard-backups"
BACKUP_KEEP_DEFAULT = 30
BACKUP_COPY_LIMIT = 100 * 1024 * 1024  # single files above this are skipped
CARD_DB_PATTERNS = (
    ("Nintendo 3DS/*/*/dbs/title.db", "title.db"),
    ("Nintendo 3DS/*/*/dbs/import.db", "import.db"),
    ("Nintendo 3DS/*/*/dbs/certs.db", "certs.db"),
)

CONSOLE_STEPS = [
    "next on the 3ds:",
    " 1. eject the card properly (don't just yank it)",
    " 2. homebrew launcher -> custom-install-finalize (~20 seconds)",
    " 3. reboot, test the game",
]

SAVE3DS_HINT = ("save3ds: build https://github.com/wwylele/save3ds and put "
                "save3ds_fuse on PATH (see its README)")

DECRYPTOR_HINT = ("decryptor: `./install.sh` builds the vendored decryptor "
                  "(decryptor/README.md) and puts decryptor-tui on PATH")


# --------------------------------------------------------------------------- #
# setup probing
# --------------------------------------------------------------------------- #

def _env_file(name: str) -> str | None:
    val = os.environ.get(name, "").strip()
    return val if val and os.path.isfile(val) else None


def _usable(path) -> str | None:
    """empty files count as missing (truncated/copied-wrong dumps)."""
    try:
        return str(path) if path and os.path.isfile(path) \
            and os.path.getsize(path) > 0 else None
    except OSError:
        return None


def find_file(name: str, env_name: str = "") -> str | None:
    if env_name:
        hit = _usable(_env_file(env_name))
        if hit:
            return hit
    for d in KEY_DIRS:
        hit = _usable(Path(d).expanduser() / name)
        if hit:
            return str(hit)
    return _usable(shutil.which(name))


def find_tool(name: str, extra_candidates=()) -> str | None:
    for candidate in extra_candidates:
        if not candidate:
            continue
        hit = _usable(Path(candidate).expanduser())
        if hit:
            return str(hit)
    hit = shutil.which(name)
    if hit:
        return _usable(hit)
    return _usable(Path("~/.local/bin").expanduser() / name)


def probe(ctx: dict) -> dict:
    ctx["boot9"] = find_file("boot9.bin", "BOOT9_PATH")
    ctx["seeddb"] = find_file("seeddb.bin", "SEEDDB_PATH")
    ctx["movable"] = find_file("movable.sed")
    ctx["save3ds"] = find_tool("save3ds_fuse",
                               (os.environ.get("CUSTOM_INSTALL_SAVE3DS_PATH", ""),))
    ctx["installer"] = find_tool("custominstall",
                                 ("~/.venvs/custom-install/bin/custominstall",))
    ctx["decryptor"] = find_tool("decryptor-tui",
                                 ("~/.cargo/bin/decryptor-tui",))
    return ctx


def sd_candidates() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    try:
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as f:
            mounts = [line.split() for line in f if line.strip()]
    except OSError:
        return out
    for parts in mounts:
        if len(parts) < 3:
            continue
        _dev, mnt, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
        if fstype not in FAT_TYPES or mnt in seen:
            continue
        seen.add(mnt)
        is_media = mnt.startswith(SD_MOUNT_PREFIXES)
        if not is_media and not mnt.startswith("/mnt/"):
            continue
        try:
            free = shutil.disk_usage(mnt).free
        except OSError:
            free = -1
        mnt_opts = parts[3].split(",") if len(parts) > 3 else []
        out.append({"mount": mnt, "fs": fstype, "free": free, "media": is_media,
                    "ro": "ro" in mnt_opts})
    out.sort(key=lambda c: (not c["media"], c["mount"]))
    return out


def tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


# --------------------------------------------------------------------------- #
# auto backups (into the user's documents folder, every run)
# --------------------------------------------------------------------------- #

def documents_dir() -> str:
    """the user's documents folder (plain names first — some xdg setups
    happily claim $HOME itself — then xdg, then a least-bad fallback)."""
    env = os.environ.get("IHC_BACKUP_DIR", "").strip()
    if env:
        cand = os.path.expandvars(os.path.expanduser(env))
        if cand and os.path.isdir(cand):
            return cand
    home = os.path.expanduser("~")
    for name in ("Documents", "Dokumente", "documents"):
        cand = os.path.join(home, name)
        if os.path.isdir(cand):
            return cand
    try:
        out = subprocess.run(["xdg-user-dir", "DOCUMENT"],
                             capture_output=True, text=True, timeout=5)
        got = out.stdout.strip()
        if out.returncode == 0 and got and os.path.isdir(got) \
                and os.path.realpath(got) != os.path.realpath(home):
            return got
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.path.join(home, ".local", "share")  # least-bad fallback


def backup_keep_count() -> int:
    try:
        return max(1, int(os.environ.get("IHC_BACKUP_KEEP", BACKUP_KEEP_DEFAULT)))
    except ValueError:
        return BACKUP_KEEP_DEFAULT


def card_backup_root() -> str:
    base = os.path.join(documents_dir(), BACKUP_DIRNAME)
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        return ""
    return base


def prune_backups(base: str) -> None:
    """keep only the newest few snapshots, folders and files both count."""
    if not base or not os.path.isdir(base):
        return
    names = sorted((n for n in os.listdir(base) if n[:1].isdigit()), reverse=True)
    for old in names[backup_keep_count():]:
        victim = os.path.join(base, old)
        try:
            if os.path.isdir(victim) and not os.path.islink(victim):
                shutil.rmtree(victim, ignore_errors=True)
            else:
                os.remove(victim)
        except OSError:
            pass


def _copy_within(src: str, dest_dir: str, name: str) -> bool:
    try:
        if os.path.getsize(src) > BACKUP_COPY_LIMIT:
            return False
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(src, os.path.join(dest_dir, name))
        return True
    except OSError:
        return False


def auto_backup(ctx: dict) -> str:
    """snapshot the card's small, hard-to-regenerate state (dbs + cifinish)."""
    sd = ctx.get("sd")
    if not sd:
        return ""
    base = ctx.get("backup_root") or card_backup_root()
    if not base:
        return ""
    dest = os.path.join(base, time.strftime("%Y%m%d-%H%M%S"))
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError:
        return ""
    copied: list[str] = []
    for pattern, name in CARD_DB_PATTERNS:
        for i, src in enumerate(glob.glob(os.path.join(sd, pattern))):
            if _copy_within(src, dest, name if i == 0 else f"{i}-{name}"):
                copied.append(name)
    card_cifinish = os.path.join(sd, "cifinish.bin")
    if os.path.isfile(card_cifinish) and _copy_within(card_cifinish, dest, "cifinish.bin"):
        copied.append("cifinish.bin")
    if not copied:  # not a 3ds card layout → don't leave empty noise dirs
        try:
            os.rmdir(dest)
        except OSError:
            pass
        return ""
    try:
        with open(os.path.join(dest, "snapshot.txt"), "w", encoding="utf-8") as f:
            f.write("auto snapshot taken when the tui started (before it touched anything)\n")
            f.write(f"mount: {sd}\n")
            f.write(f"files: {', '.join(dict.fromkeys(copied))}\n")
    except OSError:
        pass
    prune_backups(base)
    return dest


def quick_backup_file(path: str, label: str) -> str:
    """copy one file to its own backup dir; returns the dir or ''."""
    base = card_backup_root()
    if not base:
        return ""
    dest = os.path.join(base, time.strftime("%Y%m%d-%H%M%S") + "-" + label)
    if _copy_within(path, dest, os.path.basename(path)):
        prune_backups(base)
        return dest
    return ""


def backup_saves(ctx: dict) -> str:
    """copy every .sav on the card (runs before --overwrite-saves can eat them)."""
    sd = ctx.get("sd")
    if not sd:
        return ""
    base = ctx.get("backup_root") or card_backup_root()
    if not base:
        return ""
    pattern = os.path.join(sd, "Nintendo 3DS", "*", "*", "title", "*", "*",
                           "data", "**", "*.sav")
    hits = glob.glob(pattern, recursive=True)
    if not hits:
        return ""
    dest = os.path.join(base, time.strftime("%Y%m%d-%H%M%S") + "-saves")
    rel_root = os.path.join(sd, "Nintendo 3DS")
    copied = 0
    for src in hits:
        rel = os.path.relpath(src, rel_root)
        if _copy_within(src, os.path.join(dest, os.path.dirname(rel)),
                        os.path.basename(rel)):
            copied += 1
    if not copied:
        return ""
    prune_backups(base)
    return dest


def backup_dbs(ctx: dict) -> str:
    """raw copy of the card's database files before the uninstall flow rewrites
    them. why so dramatic: save3ds import() clears the whole archive first, so
    if anything goes wrong mid-import the only way back is the raw file."""
    sd = ctx.get("sd")
    if not sd:
        return ""
    base = ctx.get("backup_root") or card_backup_root()
    if not base:
        return ""
    dest = os.path.join(base, time.strftime("%Y%m%d-%H%M%S") + "-dbs-pre-uninstall")
    copied = []
    for pattern, name in CARD_DB_PATTERNS:
        for i, src in enumerate(glob.glob(os.path.join(sd, pattern))):
            if _copy_within(src, dest, name if i == 0 else f"{i}-{name}"):
                copied.append(name)
    if not copied:
        return ""
    try:
        with open(os.path.join(dest, "restore.txt"), "w", encoding="utf-8") as f:
            f.write("taken before an uninstall run changed title.db\n"
                    "roll back by copying the db files back to\n"
                    "Nintendo 3DS/<id0>/<id1>/dbs/ on the card (nothing else needed).\n")
    except OSError:
        pass
    prune_backups(base)
    return dest


def write_inventory(ctx: dict, entries: list[dict]) -> None:
    """drop the titles table in the backup dir as a timestamped text file."""
    base = ctx.get("backup_root") or card_backup_root()
    if not entries or not base:
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with open(os.path.join(base, f"{stamp}-titles.txt"), "w", encoding="utf-8") as f:
        f.write(f"# titles on {ctx.get('sd') or '?'} at {stamp}\n")
        f.write("\n".join(titles_table(entries)))
        f.write("\n")


def status_header(ctx: dict) -> list[str]:
    def fmt(p: str | None) -> str:
        return tilde(p) if p else "MISSING"
    bdir = ctx.get("backup_dir") or ctx.get("backup_root") or ""
    return [
        f"boot9      : {fmt(ctx['boot9'])}",
        f"movable.sed: {fmt(ctx['movable'])}",
        f"seeddb     : {fmt(ctx['seeddb'])}",
        f"save3ds    : {fmt(ctx['save3ds'])}",
        f"installer  : {fmt(ctx['installer'])}",
        f"decryptor  : {fmt(ctx['decryptor'])}",
        f"sd card    : {fmt(ctx['sd'])}",
        f"backups    : {tilde(bdir) if bdir else 'off'}",
    ]


# --------------------------------------------------------------------------- #
# card listings
# --------------------------------------------------------------------------- #

def extract_titles(ctx: dict) -> list[dict]:
    """extract the card's title.db via save3ds and decode the entries.

    note the save3ds_fuse argument shape (learned the hard way documented in
    docs/quirks-and-fixes.md): -x is a flag, the destination dir is the one
    positional, and boot9 is required even for sd databases.
    """
    tmp = tempfile.mkdtemp(prefix="ci-card-list-")
    try:
        cmd = [ctx["save3ds"], "--db", "sdtitle", "-x",
               "--sd", ctx["sd"], "-m", ctx["movable"], "-b", ctx["boot9"], tmp]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "unknown failure").strip()
            raise RuntimeError(tail.splitlines()[-1] if tail else "unknown failure")
        entries: list[dict] = []
        for name in os.listdir(tmp):
            if not all(c in "0123456789abcdefABCDEF" for c in name) or len(name) != 16:
                continue
            try:
                with open(os.path.join(tmp, name), "rb") as f:
                    fields = decode_entry(f.read(128))
            except (OSError, ValueError):
                continue
            fields["tid"] = name.lower()
            entries.append(fields)
        entries.sort(key=lambda e: int(e["tid"], 16))
        return entries
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def titles_table(entries: list[dict]) -> list[str]:
    if not entries:
        return ["(nothing found in the card's title.db)"]
    rows = [f"{'#':>3}  {'product code':<13}{'title id':18s}{'size':>10s}  manual"]
    for i, e in enumerate(entries, 1):
        code = e["code_text"] or "-"
        mib = e["size_on_sd"] / (1024 * 1024)
        manual = "yes" if e["flags0"] & 0x1 else "no"
        rows.append(f"{i:>3}. {code:<13}{e['tid']}  {mib:>8.1f} M  {manual}")
    return rows


def cache_titles(ui, ctx: dict) -> list[dict]:
    cached = ctx.get("titles")
    if cached is not None and ctx.get("titles_sd") == ctx["sd"]:
        return cached
    print("reading the card's title.db (takes a moment)...")
    entries = ui.run_visible(lambda: extract_titles(ctx))
    ctx["titles"] = entries
    ctx["titles_sd"] = ctx["sd"]
    try:
        write_inventory(ctx, entries)  # history of what the card held, per run
    except OSError:
        pass
    return entries


def sd_id0(movable_path: str) -> str | None:
    """the id0 directory name the console derives from movable.sed.

    same derivation as save3ds' hash_movable (verified against pyctr): sha256
    over the 16-byte seed at offset 0x110 of the movable, then the four 4-byte
    groups of the digest reversed."""
    try:
        with open(movable_path, "rb") as f:
            f.seek(0x110)
            seed = f.read(16)
    except OSError:
        return None
    if len(seed) != 16:
        return None
    digest = hashlib.sha256(seed).digest()
    return bytes(digest[3::-1] + digest[7:3:-1]
                 + digest[11:7:-1] + digest[15:11:-1]).hex()


def card_content_dirs(ctx: dict) -> list[str]:
    """Nintendo 3DS/<id0>/<id1>/ dirs on the chosen card (id0 derived from the
    movable exactly like the console and save3ds do; <id1> dir taken as-is)."""
    movable = ctx.get("movable")
    sd = ctx.get("sd")
    if not movable or not sd:
        return []
    id0 = sd_id0(movable)
    if not id0:
        return []
    return [d for d in sorted(glob.glob(os.path.join(sd, "Nintendo 3DS", id0, "*")))
            if os.path.isdir(d)]


# --------------------------------------------------------------------------- #
# flows (shared by both ui modes)
# --------------------------------------------------------------------------- #

def gateway(ctx: dict, need=(), sd: bool = True, ui=None) -> str | None:
    """make sure sd (unless opted out) + required tools exist → error text or None."""
    problems = []
    if sd and ctx["sd"] is None:
        problems.append("no sd card picked — pick one from the main menu")
    for key in need:
        if not ctx.get(key):
            problems.append(f"{key.replace('_', '.') or key} missing — see setup in the readme")
    for key in need:
        if key == "save3ds" and not ctx.get("save3ds"):
            problems.append(SAVE3DS_HINT)
        if key == "decryptor" and not ctx.get("decryptor"):
            problems.append(DECRYPTOR_HINT)
    if problems:
        ui.page("missing pieces", problems)
        return None
    return ctx["sd"]


def pick_sd(ui, ctx: dict) -> None:
    """choose the active sd card; auto-picks when exactly one media mount shows."""
    ctx["sd"] = None
    if ctx.get("sd_confirmed"):
        return
    cands = sd_candidates()
    media = [c for c in cands if c["media"]]
    if len(media) == 1:
        ctx["sd"] = media[0]["mount"]
        free = media[0]["free"]
        human = f"{free / (1024 ** 3):.1f} GiB free" if free >= 0 else "size unknown"
        print("picked the sd card automatically: "
              f"{media[0]['mount']} ({media[0]['fs']}, {human})"
              + (" READ-ONLY MOUNT!" if media[0].get("ro") else ""))
        lines = [f"picked the sd card without asking: {media[0]['mount']}"]
        ctx["sd_note"] = lines[0]
        return
    # multiple candidates (or none) → ask
    if not cands:
        ui.page("no fat/exfat mounts found",
                ["is the card plugged in? weird fs? type the path manually below."])
    rows = [
        (f"{c['mount']}  ({c['fs']}, {c['free'] / 1024 ** 3:.1f} GiB free)"
         + (" READ-ONLY MOUNT!" if c.get("ro") else ""))
        if c["free"] >= 0 else f"{c['mount']}  ({c['fs']})"
        for c in cands
    ]
    rows.append("type a path manually")
    print("\nsd card candidates:")
    picks = ui.pick_many("sd card candidates", rows) or []
    if not picks:
        return
    # the manual row can be picked together with mount rows — manual wins
    # (v0.1 only handled an exact picks == [len(rows)], so picks like
    # [2, 4] silently dropped the manual intent)
    if len(rows) in picks:
        manual = ui.ask("sd card mount point: ", "")
        path = os.path.expandvars(os.path.expanduser(manual))
        if os.path.isdir(path):
            ctx["sd"] = path
        else:
            ui.page("not a directory", [path])
        return
    ctx["sd"] = cands[picks[0] - 1]["mount"]


def pick_dump_dir(ui) -> str | None:
    default_dir = os.getcwd()
    while True:
        raw = ui.ask(f"directory to scan for dumps [enter = {default_dir}]", "")
        if not raw:
            return default_dir
        cand = os.path.expandvars(os.path.expanduser(raw))
        if os.path.isdir(cand):
            return cand
        print(f"not a directory: {cand}")


def scan_dumps(directory: str) -> list[str]:
    dumps: list[str] = []
    for name in sorted(os.listdir(directory)):
        if name.startswith("."):
            continue
        if os.path.splitext(name)[1].lower() in DUMP_EXTS:
            dumps.append(os.path.join(directory, name))
    return dumps


def flow_install(ui, ctx: dict) -> None:
    if gateway(ctx, need=("installer", "save3ds", "movable", "boot9"), ui=ui) is None:
        return
    dir_ = pick_dump_dir(ui)
    dumps = scan_dumps(dir_)
    if not dumps:
        ui.page("nothing to install", [
            f"no .3ds/.3dz/.cia files in {dir_}",
            f"for reference, the extensions looked for are: {', '.join(DUMP_EXTS)}",
        ])
        return
    dumps = [dumps[i - 1] for i in (ui.pick_many(
        "pick dump(s) to install (" + tilde(dir_) + ")",
        [tilde(d) for d in dumps]) or [])]
    if not dumps:
        return
    overwrite = ui.confirm("overwrite existing save files?", default=False)
    # --overwrite-saves can eat real save data → auto-copy the existing .sav
    # files into the documents backup folder before the installer runs
    saves_backup = backup_saves(ctx) if overwrite else ""
    cmd = [ctx["installer"]]
    if ctx["boot9"]:
        cmd += ["-b", ctx["boot9"]]
    cmd += ["-m", ctx["movable"]]
    if ctx["seeddb"]:
        cmd += ["-s", ctx["seeddb"]]
    cmd += ["--sd", ctx["sd"]]
    if overwrite:
        cmd += ["--overwrite-saves"]
    cmd += dumps
    ui.page("running custominstall...", [
        " ".join(shlex_quote(arg) for arg in cmd),
        "its own progress output will take over the terminal.",
    ])
    rc = ui.run_visible(lambda: subprocess.run(cmd).returncode)
    lines = [f"custominstall exited with status {rc}."]
    if rc == 0:
        ctx["titles"] = None  # the card listing changed → refresh on next read
        ctx["titles_sd"] = None
        lines += [
            f"content written to {tilde(ctx['sd'])}",
            "double-check the output above for '--skip-contents' hints or",
            "per-title failures before trusting it fully.",
        ]
        if saves_backup:
            lines.append(f"old save files backed up to {tilde(saves_backup)}")
        lines += CONSOLE_STEPS
    else:
        lines += ["check the output above for the failure reason."]
        if saves_backup:
            lines.append(f"note: existing saves were backed up to {tilde(saves_backup)}")
    ui.page("install result", lines)


def flow_decrypt(ui, ctx: dict) -> None:
    """batch-decrypt dumps on the pc (no sd card involvement). originals are
    only ever read: everything runs on copies inside a work dir, and the
    decrypted files are moved back out when it succeeds."""
    if gateway(ctx, need=("decryptor",), sd=False, ui=ui) is None:
        return
    dir_ = pick_dump_dir(ui)
    dumps = scan_dumps(dir_)
    if not dumps:
        ui.page("nothing to decrypt", [
            f"no .3ds/.3dz/.cia files in {dir_}",
            f"the extensions looked for are: {', '.join(DUMP_EXTS)}",
        ])
        return
    picks = ui.pick_many("pick dump(s) to decrypt (" + tilde(dir_) + ")",
                         [tilde(d) for d in dumps])
    if not picks:
        return
    chosen = [dumps[i - 1] for i in picks]
    work = os.path.join(dir_, ".ihaveacreditcard-decrypt")
    try:
        os.makedirs(work, exist_ok=True)
        for src in chosen:
            print(f"copying {tilde(src)} "
                  f"(≈{os.path.getsize(src) / (1024 ** 3):.1f} GiB) — original untouched")
            shutil.copy2(src, os.path.join(work, os.path.basename(src)))
        cmd = [ctx["decryptor"], "--headless"]
        if ctx["seeddb"]:
            cmd += ["--seeddb", ctx["seeddb"]]
        cmd.append(work)
        ui.page("decrypting...", [
            " ".join(shlex_quote(a) for a in cmd),
            "the tool's own progress output streams below.",
        ])
        rc = ui.run_visible(lambda: subprocess.run(cmd).returncode)
    except KeyboardInterrupt:
        ui.page("decrypt interrupted", [
            f"processed files are left in {work}",
            "nothing was removed — delete the dir by hand if you want it gone.",
        ])
        return
    except OSError as exc:
        ui.page("couldn't prepare the work dir", [str(exc)])
        return
    moved: list[str] = []
    for out in sorted(glob.glob(os.path.join(work, "*-decrypted.c*"))):
        try:
            os.replace(out, os.path.join(dir_, os.path.basename(out)))
            moved.append(os.path.basename(out))
        except OSError:
            pass
    lines = [f"decryptor-tui exited with status {rc}."]
    if moved:
        lines.append("decrypted files, ready for the install flow:")
        lines += [f"  {m}" for m in moved]
    else:
        lines.append("no *-decrypted.* outputs were produced.")
    if os.path.isdir(work):
        lines += ["", f"work dir left in place: {tilde(work)} (safe to delete)"]
    ui.page("decrypt result", lines)


def save3ds_cmd(ctx: dict, mode: str, directory: str) -> list[str]:
    """the argument shape documented in docs/quirks-and-fixes.md: -x/-i are
    plain flags and the directory is the one positional argument."""
    return [ctx["save3ds"], "--db", "sdtitle", mode,
            "--sd", ctx["sd"], "-m", ctx["movable"], "-b", ctx["boot9"], directory]


def flow_uninstall(ui, ctx: dict) -> None:
    """remove title(s) from the card pc-side: prune title.db entries through
    save3ds export/import (import clears the archive and rewrites it without
    the picked ones) and delete the title content dirs. dbs + saves are
    backed up first; untouched until the export has fully succeeded."""
    if gateway(ctx, need=("save3ds", "movable", "boot9"), ui=ui) is None:
        return
    entries = cache_titles(ui, ctx)
    if not entries:
        ui.page("nothing to uninstall", titles_table(entries))
        return
    ui.page("about uninstalling", [
        "removes titles from the card's title.db pc-side and deletes their",
        "content + save data — the pc equivalent of FBI's Titles → Delete.",
        "",
        "what actually happens:",
        " 1. copy dbs + saves + cifinish into the backup folder",
        " 2. export every title.db entry to a temp dir (read-only step)",
        " 3. import() clears the whole archive, then rewrites it without the",
        "    picked entries (save3ds drops their files first)",
        " 4. delete the picked titles' content dirs on the card",
        " 5. verify by re-exporting and counting",
        "",
        "tickets are NOT touched (they live on the console's NAND) — if FBI's",
        "Tickets menu still lists one, delete it there. safe rollback: the",
        "uninstall writes a raw dbs backup first (restore instructions included).",
    ])
    picks = ui.pick_many("pick title(s) to uninstall",
                         titles_table(entries)[1:])  # titles_table has a header row
    if not picks:
        return
    picked = [entries[i - 1] for i in picks]
    names = [e["code_text"] or e["tid"] for e in picked]
    dbs_backup = backup_dbs(ctx)
    if not dbs_backup:
        ui.page("refusing to run without the backup", [
            "couldn't copy title.db/import.db into the backup folder —",
            "fix IHC_BACKUP_DIR or the documents folder and try again.",
        ])
        return
    saves_backup = backup_saves(ctx)
    if not ui.confirm(
            f"really uninstall {len(picked)} title(s)? ({', '.join(names)})",
            default=False):
        ui.page("aborted — nothing was touched", [
            f"the dbs backup is still in {tilde(dbs_backup)} (harmless leftover)"])
        return
    tmp = tempfile.mkdtemp(prefix="ihaveacreditcard-uninstall-")
    kept_dir = os.path.join(tmp, "kept")
    removed_dir = os.path.join(tmp, "removed")
    verify_dir = os.path.join(tmp, "verify")
    os.makedirs(kept_dir)
    os.makedirs(removed_dir)
    os.makedirs(verify_dir)
    keep_tmp = False
    try:
        proc = subprocess.run(save3ds_cmd(ctx, "-x", kept_dir),
                              capture_output=True, text=True)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "unknown failure").strip()
            ui.page("export failed — nothing was touched", [
                tail.splitlines()[-1] if tail else "unknown failure",
                "",
                "a signature mismatch here means the card's dbs are in an",
                "inconsistent state (mid-write earlier?): reboot the console",
                "once with the card in, then run this again.",
            ])
            return
        missing: list[str] = []
        for tid in [e["tid"] for e in picked]:
            entry_file = os.path.join(kept_dir, tid)
            if os.path.isfile(entry_file):
                os.replace(entry_file, os.path.join(removed_dir, tid))
            else:
                missing.append(tid)
        if missing == [e["tid"] for e in picked]:
            ui.page("nothing matched", [
                "none of the picked titles were found in the exported entries",
                "(weird — they were just listed). nothing was changed.",
            ])
            return
        proc = subprocess.run(save3ds_cmd(ctx, "-i", kept_dir),
                              capture_output=True, text=True)
        if proc.returncode != 0:
            keep_tmp = True
            tail = (proc.stderr or proc.stdout or "unknown failure").strip()
            ui.page("the db rewrite FAILED", [
                tail.splitlines()[-1] if tail else "unknown failure",
                "",
                "the card's dbs may now be incomplete or broken. restore:",
                f"  1. copy title.db (and import.db) from {tilde(dbs_backup)}",
                "     onto the card: Nintendo 3DS/<id0>/<id1>/dbs/",
                f"  2. the pruned export is kept at {tmp}/kept",
            ])
            return
        deleted = []
        for tid in [e["tid"] for e in picked]:
            for base in card_content_dirs(ctx):
                victim = os.path.join(base, "title", tid[:8], tid[8:])
                if os.path.isdir(victim):
                    shutil.rmtree(victim, ignore_errors=True)
                    deleted.append(victim)
        proc = subprocess.run(save3ds_cmd(ctx, "-x", verify_dir),
                              capture_output=True, text=True)
        survivors = [e["tid"] for e in picked
                     if os.path.isfile(os.path.join(verify_dir, e["tid"]))]
        total = len([n for n in os.listdir(verify_dir)
                     if len(n) == 16 and all(c in "0123456789abcdefABCDEF" for c in n)]) \
            if proc.returncode == 0 else -1
        ctx["titles"] = None  # the card listing changed → refresh on next read
        ctx["titles_sd"] = None
        lines = [
            f"removed {len(picked) - len(survivors)}/{len(picked)} title(s) from title.db"
            + (f" (verify failed: {proc.returncode})" if proc.returncode else ""),
            f"title.db now lists {total} entr{'y' if total == 1 else 'ies'}"
            if total >= 0 else "verify re-export failed — check things over in the card list flow",
            f"content dirs deleted: {len(deleted)}"
            + ("" if len(deleted) == len(picked) else " — some titles had none (system entries?)"),
            "",
            f"dbs backup (rollback): {tilde(dbs_backup)}",
        ]
        if saves_backup:
            lines.append(f"save data snapshot: {tilde(saves_backup)}")
        if survivors:
            lines += ["", "these are STILL in the db (import didn't take them out):"]
            lines += [f"  {t}" for t in survivors]
        lines += ["",
                  "open the HOME menu again (or reboot the console) to see the change;"
                  " leftover tickets can only be deleted on console via FBI → Tickets."]
        ui.page("uninstall done", lines)
    finally:
        if keep_tmp:
            ui.page("note", [f"debug files kept: {tmp}"])
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def shlex_quote(text: str) -> str:
    import shlex
    return shlex.quote(text)


def flow_list(ui, ctx: dict) -> None:
    if gateway(ctx, need=("save3ds", "movable", "boot9"), ui=ui) is None:
        return
    entries = cache_titles(ui, ctx)
    ui.page(f"titles on {tilde(ctx['sd'])}", titles_table(entries))


def read_existing_seeds(path: str) -> dict[int, bytes]:
    """pull seeds out of an existing cifinish.bin (v3 layout) if one is there."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    seeds: dict[int, bytes] = {}
    if raw[:8] != b"CIFINISH":
        return seeds
    try:
        version = int.from_bytes(raw[8:12], "little")
        count = int.from_bytes(raw[12:16], "little")
        if version != 3:
            return seeds
        for i in range(count):
            entry = raw[0x10 + i * 0x20: 0x10 + (i + 1) * 0x20]
            if len(entry) != 0x20 or entry[:6] != b"TITLE\0":
                break
            if entry[6]:
                seeds[int.from_bytes(entry[8:16], "little")] = entry[0x10:]
    except (ValueError, IndexError):
        pass
    return seeds


def flow_cifinish(ui, ctx: dict) -> None:
    if gateway(ctx, need=("save3ds", "movable", "boot9"), ui=ui) is None:
        return
    entries = cache_titles(ui, ctx)
    if not entries:
        ui.page("nothing to re-finalize", titles_table(entries))
        return
    rows = titles_table(entries)[1:]  # titles_table prepends a header row
    picks = ui.pick_many("pick title(s) to re-finalize", rows)
    if not picks:
        return
    tids = [int(entries[i - 1]["tid"], 16) for i in picks]
    default_out = os.path.join(ctx["sd"], "cifinish.bin")
    out = os.path.expandvars(os.path.expanduser(ui.ask("output path", default_out)))
    # whatever is being replaced gets copied to the documents backup first
    backed = quick_backup_file(out, "cifinish") if os.path.exists(out) else ""
    if backed and not ui.confirm(
            f"{out} exists — overwrite? (old copy → {tilde(backed)})", default=False):
        return
    # carry over the seeds a previous custom-install run stored, so a
    # seed-encrypted title doesn't lose its seed on re-finalize
    seeds = {t: s for t, s in read_existing_seeds(out).items() if t in tids}
    data = build_cifinish(tids, seeds)
    with open(out, "wb") as f:
        f.write(data)
    note = f" ({len(seeds)} seed(s) carried over)" if seeds else ""
    lines = [f"wrote {len(data)} bytes to {out} for {len(tids)} title(s){note}"]
    if backed:
        lines.append(f"previous file backed up to {tilde(backed)}")
    ui.page("cifinish.bin written", lines + [""] + CONSOLE_STEPS)


def flow_inspect(ui, ctx: dict) -> None:
    if gateway(ctx, need=("save3ds", "movable", "boot9"), ui=ui) is None:
        return
    entries = cache_titles(ui, ctx)
    by_tid = {e["tid"]: e for e in entries}
    raw = ui.ask("title id (16 hex digits, empty = cancel)", "")
    if not raw:
        return
    try:
        tid = f"{int(raw, 16):016x}"
    except ValueError:
        ui.page("that's not a title id", [raw])
        return
    entry = by_tid.get(tid)
    if entry is None:
        ui.page("not on this card", [f"{tid} isn't in the card's title.db"])
        return
    ui.page(f"title {tid}", entry_lines(tid, entry))


def flow_toolchain(ui, ctx: dict) -> None:
    venv = os.path.expanduser("~/.venvs/custom-install")
    pip = os.path.join(venv, "bin", "pip")
    def _do() -> int:
        rc = subprocess.run([sys.executable, "-m", "venv", venv]).returncode
        if rc == 0 and os.path.exists(pip):
            rc = subprocess.run([pip, "install", "--upgrade", PACKAGE_URL]).returncode
        return rc
    if not ui.confirm(
        "create/upgrade the venv + pip install custom-install now? "
        f"(needs internet) — python is {sys.executable}", default=True):
        ui.page("skipped", ["you can do it manually, see the readme's setup section"])
        return
    rc = ui.run_visible(_do)
    probe(ctx)
    status = "ssh, it worked" if rc == 0 and ctx["installer"] else f"setup exited with {rc}"
    if rc == 0 and ctx["installer"]:
        status += f"\ninstaller now: {tilde(ctx['installer'])}"
    ui.page("toolchain", [status] + (CONSOLE_STEPS if rc == 0 else []))


def flow_check(ui, ctx: dict) -> None:
    """the same doctor that --check runs, but inside the menus."""
    import doctor
    findings, warns, fails = doctor.collect()
    ui.page("setup check", doctor.page_lines(findings, warns, fails))


FLOWS = {
    "install": flow_install,
    "decrypt": flow_decrypt,
    "list": flow_list,
    "uninstall": flow_uninstall,
    "cifinish": flow_cifinish,
    "inspect": flow_inspect,
    "toolchain": flow_toolchain,
    "check": flow_check,
}


def main_options(_ctx: dict) -> list[tuple[str, str]]:
    return [
        ("install game dump(s) onto the sd card", "install"),
        ("decrypt dump(s) on the pc (no sd card needed)", "decrypt"),
        ("list titles installed on the card", "list"),
        ("uninstall title(s) from the card", "uninstall"),
        ("build cifinish.bin (re-finalize helper)", "cifinish"),
        ("inspect a title entry (pc-side check)", "inspect"),
        ("install the python toolchain (venv + pip)", "toolchain"),
        ("run a setup check (doctor)", "check"),
        ("rescan keys / tools / sd cards", "rescan"),
        ("quit", "quit"),
    ]


def run_app(ui, ctx: dict) -> None:
    probe(ctx)
    ctx["backup_root"] = card_backup_root() or ctx.get("backup_root") or ""
    if ctx["sd"] is None:
        pick_sd(ui, ctx)
        ctx["sd_confirmed"] = True
    if ctx["sd"]:  # one snapshot per launch: dbs + cifinish.bin, cheap files only
        ctx["backup_dir"] = auto_backup(ctx)
    while True:
        opts = main_options(ctx)
        idx = ui.menu(APP_TITLE, [label for label, _ in opts], header=status_header(ctx))
        if idx is None:
            return
        action = opts[idx][1]
        if action == "quit":
            return
        if action == "rescan":
            ctx["sd"] = None
            probe(ctx)
            ctx["sd_confirmed"] = False
            pick_sd(ui, ctx)
            ctx["sd_confirmed"] = True
            if ctx["sd"]:  # card changed? snapshot the new one too
                ctx["backup_dir"] = auto_backup(ctx)
            continue
        try:
            FLOWS[action](ui, ctx)
        except KeyboardInterrupt:
            continue
        except Exception as exc:  # keep the tui alive no matter what breaks
            fmt_exc = type(exc).__name__ + (f": {exc}" if str(exc) else "")
            ui.page("that one failed", [fmt_exc])


# --------------------------------------------------------------------------- #
# ui backends
# --------------------------------------------------------------------------- #

class PlainIO:
    """plain-text terminal io variant, works on pipes + dumb terminals."""

    def _suspend(self) -> None:  # no-op for the plain backend
        pass

    def _resume(self) -> None:  # no-op for the plain backend
        pass

    def ask(self, prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        try:
            line = input(f"{prompt}{suffix} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        return line or default

    def confirm(self, prompt: str, default: bool = False) -> bool:
        suffix = "y/N" if default else "N/y"
        try:
            line = input(f"{prompt} ({suffix}) ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not line:
            return default
        return line[0] in ("y", "a")

    def pause(self) -> None:
        try:
            input("— enter to go back —")
        except (EOFError, KeyboardInterrupt):
            print()

    def page(self, title: str, lines) -> None:
        self._suspend()
        try:
            print()
            print(f"== {title} ==")
            for line in lines:
                print(f"  {line}")
            self.pause()
        finally:
            self._resume()

    def menu(self, title: str, options: list[str], header=()):
        self._suspend()
        try:
            print()
            print(f"== {title} ==")
            for line in header:
                print(f"  {line}")
            for i, label in enumerate(options, 1):
                print(f" {i}. {label}")
            while True:
                try:
                    raw = input("? ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return None
                if raw in ("q", "x", "quit", "exit"):
                    return None
                try:
                    idx = int(raw)
                except ValueError:
                    print("give a number between 1 and %d (or q)" % len(options))
                    continue
                if 1 <= idx <= len(options):
                    return idx - 1
                print("that number's out of range (or q to quit)")
        finally:
            self._resume()

    def pick_many(self, title: str, rows: list[str]) -> list[int] | None:
        self._suspend()
        try:
            print(f"\n== {title} ==")
            for i, row in enumerate(rows, 1):
                print(f" {i:>3}.  {row}")
            while True:
                try:
                    line = input(
                        "numbers separated by spaces | a = all | empty = cancel: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return None
                if line.lower() == "a":
                    return list(range(1, len(rows) + 1))
                if not line:
                    return None
                picks: list[int] = []
                ok = True
                for token in line.replace(",", " ").split():
                    try:
                        idx = int(token)
                    except ValueError:
                        ok = False
                        break
                    if not 1 <= idx <= len(rows):
                        ok = False
                        break
                    if idx not in picks:
                        picks.append(idx)
                if ok and picks:
                    return picks
                print(f"give numbers between 1 and {len(rows)}, or 'a', or empty to cancel")
        finally:
            self._resume()

    def run_visible(self, fn):
        # nothing to suspend in plain mode; output just flows to the terminal
        self._suspend()
        try:
            return fn()
        finally:
            self._resume()


class CursesIO(PlainIO):
    """curses full-screen menus; dialogs drop back to the real terminal."""

    def __init__(self, std):
        import curses  # local import so --plain never needs it
        self._curses = curses
        self.std = std
        self._setup()

    def _setup(self) -> None:
        c = self._curses
        try:
            c.curs_set(0)
        except c.error:
            pass
        try:
            c.start_color()
            c.use_default_colors()
            # 1 green (ok lines), 2 cyan (titles), 3 red (failures),
            # 4 yellow (warnings), 5 magenta (echo/notes)
            c.init_pair(1, c.COLOR_GREEN, -1)
            c.init_pair(2, c.COLOR_CYAN, -1)
            c.init_pair(3, c.COLOR_RED, -1)
            c.init_pair(4, c.COLOR_YELLOW, -1)
            c.init_pair(5, c.COLOR_MAGENTA, -1)
        except c.error:
            pass
        self.std.keypad(True)
        self._colors = c.has_colors()

    def _suspend(self) -> None:
        self._curses.def_prog_mode()
        self._curses.endwin()

    def _resume(self) -> None:
        self._curses.reset_prog_mode()
        self.std.clear()
        self.std.refresh()

    def ask(self, prompt: str, default: str = "") -> str:
        self._suspend()
        try:
            return super().ask(prompt, default)
        finally:
            self._resume()

    def confirm(self, prompt: str, default: bool = False) -> bool:
        self._suspend()
        try:
            return super().confirm(prompt, default)
        finally:
            self._resume()

    def _size(self) -> tuple[int, int]:
        try:
            return self.std.getmaxyx()
        except self._curses.error:
            return 24, 80

    def page(self, title: str, lines) -> None:
        """full-screen scrollable page view (falls back to the plain dialog
        only when the terminal is too cramped for real 8×30 + rows)."""
        c = self._curses
        lines = [str(line) for line in lines] or ["(nothing to show)"]
        rows, cols = self._size()
        if rows < 8 or cols < 30:
            self._suspend()
            try:
                super().page(title, lines)
            finally:
                self._resume()
            return
        scroll = 0
        while True:
            rows, cols = self._size()
            self.std.erase()

            def put(r: int, cc: int, text: str, attr=c.A_NORMAL) -> None:
                if 0 <= r < rows and 0 <= cc < cols:
                    self.std.addnstr(r, cc, text, max(0, cols - cc), attr)

            tattr = c.A_BOLD | (c.color_pair(2) if self._colors else 0)
            put(0, 1, title, tattr)
            put(1, 1, "↑/↓, pgup/pgdn, home/end scroll — any other key = back", c.A_DIM)
            view_h = max(1, rows - 4)
            max_scroll = max(0, len(lines) - view_h)
            scroll = max(0, min(scroll, max_scroll))
            for i, line in enumerate(lines[scroll:scroll + view_h]):
                attr = c.A_NORMAL
                if self._colors:
                    if "FAIL" in line:
                        attr = c.color_pair(3) | c.A_BOLD
                    elif "MISSING" in line:
                        attr = c.color_pair(4)
                    elif line.startswith("ok "):
                        attr = c.color_pair(1) | c.A_BOLD
                    elif line.startswith("warn "):
                        attr = c.color_pair(4) | c.A_BOLD
                    elif line.startswith("== ") or line.startswith("-- "):
                        attr = c.color_pair(5) | c.A_BOLD
                put(2 + i, 1, line, attr)
            if max_scroll > 0:
                window = (f"{scroll + 1}-{min(scroll + view_h, len(lines))}"
                          f"/{len(lines)}")
                put(rows - 1, 1, window + ("  (more below)" if scroll < max_scroll else ""),
                    c.A_DIM)
            self.std.refresh()
            try:
                key = self.std.getch()
            except KeyboardInterrupt:
                return
            if key in (c.KEY_UP, ord("k")):
                scroll -= 1
            elif key in (c.KEY_DOWN, ord("j")):
                scroll += 1
            elif key in (c.KEY_PPAGE,):
                scroll -= view_h
            elif key in (c.KEY_NPAGE,):
                scroll += view_h
            elif key == c.KEY_HOME:
                scroll = 0
            elif key == c.KEY_END:
                scroll = max_scroll
            else:
                return

    def pick_many(self, title: str, rows: list[str]) -> list[int] | None:
        """full-screen checkbox picker: space toggles, a toggles all/none,
        enter confirms. returns 1-based indices (plain-fallback contract)."""
        c = self._curses
        rows, cols = self._size()
        if rows < 8 or cols < 30:
            self._suspend()
            try:
                return super().pick_many(title, rows)
            finally:
                self._resume()
        checked: set[int] = set()  # 0-based row indexes
        sel = 0
        while True:
            rows, cols = self._size()
            self.std.erase()

            def put(r: int, cc: int, text: str, attr=c.A_NORMAL) -> None:
                if 0 <= r < rows and 0 <= cc < cols:
                    self.std.addnstr(r, cc, text, max(0, cols - cc), attr)

            tattr = c.A_BOLD | (c.color_pair(2) if self._colors else 0)
            put(0, 1, title, tattr)
            put(1, 1, "space toggle · a all/none · enter done · anything else = cancel", c.A_DIM)
            list_top = 2
            list_h = max(1, rows - list_top - 2)
            start = max(0, min(sel - list_h // 2, len(rows) - list_h))
            for j in range(len(rows)):
                if j - start >= list_h:
                    put(rows - 2, 1, "… (more lines)", c.A_DIM)
                    break
                box = "[x]" if j in checked else "[ ]"
                attr = (c.A_BOLD if j in checked else c.A_NORMAL)
                if self._colors and j in checked:
                    attr |= c.color_pair(1)
                if j == sel:
                    attr |= c.A_REVERSE
                put(list_top + j - start, 1, f"{box} {rows[j]}", attr)
            put(rows - 1, 1, f"{len(checked)}/{len(rows)} selected — enter = go", c.A_DIM)
            self.std.refresh()
            try:
                key = self.std.getch()
            except KeyboardInterrupt:
                return None
            if key in (c.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif key in (c.KEY_DOWN, ord("j")):
                sel = min(len(rows) - 1, sel + 1)
            elif key in (c.KEY_PPAGE,):
                sel = max(0, sel - list_h)
            elif key in (c.KEY_NPAGE,):
                sel = min(len(rows) - 1, sel + list_h)
            elif key == c.KEY_HOME:
                sel = 0
            elif key == c.KEY_END:
                sel = len(rows) - 1
            elif key == 32:  # space
                checked.symmetric_difference_update({sel})
            elif key == ord("a"):
                if len(checked) == len(rows):
                    checked.clear()
                else:
                    checked = set(range(len(rows)))
            elif key in (c.KEY_ENTER, 10, 13, c.KEY_RIGHT):
                return sorted(i + 1 for i in checked) or None
            else:  # q/esc/unknown → same contract as the plain fallback: cancel
                return None

    def menu(self, title: str, options: list[str], header=()):
        c = self._curses
        sel = 0
        while True:
            try:
                rows, cols = self.std.getmaxyx()
            except c.error:
                rows, cols = 24, 80
            if rows < 6 or cols < 30:
                k = self.std.getch()
                if k in (ord("q"), 27):
                    return None
                continue
            self.std.erase()
            def put(r: int, cc: int, text: str, attr=c.A_NORMAL) -> None:
                if 0 <= r < rows and 0 <= cc < cols:
                    self.std.addnstr(r, cc, text, max(0, cols - cc), attr)
            title_attr = c.A_BOLD
            if self._colors:
                title_attr |= c.color_pair(2)
            put(0, 1, title, title_attr)
            for i, line in enumerate(header):
                dim = c.A_DIM if not (self._colors and "MISSING" not in line) \
                    else (c.color_pair(1) | c.A_BOLD)
                put(2 + i, 3, line, dim)
            row = 2 + len(header) + 1
            visible = max(1, rows - row - 2)
            start = max(0, min(sel - visible // 2, len(options) - visible))
            for j in range(len(options)):
                if j - start >= visible:
                    put(rows - 2, 1, "…", c.A_DIM)
                    break
                mark = "→ " if j == sel else "  "
                attr = c.A_REVERSE if j == sel else c.A_NORMAL
                put(row + j - start, 1, mark + options[j], attr)
            put(rows - 1, 1, "↑/↓ move · enter select · q quit", c.A_DIM)
            self.std.refresh()
            try:
                key = self.std.getch()
            except KeyboardInterrupt:  # ctrl-c = cancel, same as q
                return None
            if key in (c.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif key in (c.KEY_DOWN, ord("j")):
                sel = min(len(options) - 1, sel + 1)
            elif key in (c.KEY_PPAGE,):
                sel = max(0, sel - visible)
            elif key in (c.KEY_NPAGE,):
                sel = min(len(options) - 1, sel + visible)
            elif key in (c.KEY_HOME,):
                sel = 0
            elif key in (c.KEY_END,):
                sel = len(options) - 1
            elif key in (c.KEY_ENTER, 10, 13, c.KEY_RIGHT):
                return sel
            elif key in (ord("q"), 27, c.KEY_LEFT):
                return None


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    plain = "--plain" in argv or "-p" in argv \
        or not (sys.stdin.isatty() and sys.stdout.isatty())
    ctx: dict = {
        "sd": None,
        "sd_confirmed": False,
        "titles": None,
        "titles_sd": None,
    }
    if "--check" in argv:  # non-interactive self-check, sane exit codes
        import doctor
        return doctor.main(argv)
    if plain:
        try:
            run_app(PlainIO(), ctx)
        except KeyboardInterrupt:
            print()
            return 130
        return 0
    try:
        import curses
        curses.wrapper(_curses_run, ctx)
        return 0
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as exc:  # broken terminal etc → fall back to plain menus
        if "--plain-strict" in argv:
            raise
        print(f"curses died ({exc!r}); falling back to plain menus", file=sys.stderr)
        try:
            run_app(PlainIO(), ctx)
        except KeyboardInterrupt:
            pass
        return 0


def _curses_run(std, ctx: dict) -> None:
    run_app(CursesIO(std), ctx)


if __name__ == "__main__":
    sys.exit(main())
