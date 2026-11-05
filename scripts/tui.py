#!/usr/bin/env python3
"""tui for the toolkit: check the setup, install dumps, list card titles, write cifinish.bin.

run from inside the repo:

    python3 scripts/tui.py

it drives the exact same tools documented in the README (the custominstall
binary, save3ds_fuse, boot9/movable keys) — no crypto is reimplemented here.

if the terminal can't do full-screen rendering (or you pass --plain), it falls
back to a plain numbered menu that offers the exact same functions.
"""

from __future__ import annotations

import glob
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

APP_TITLE = "ihaveacreditcard · 3ds sd install toolkit (v0.1)"
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


# --------------------------------------------------------------------------- #
# flows (shared by both ui modes)
# --------------------------------------------------------------------------- #

def gateway(ctx: dict, need=(), ui=None) -> str | None:
    """make sure sd + required tools exist. returns error text or None."""
    problems = []
    if ctx["sd"] is None:
        problems.append("no sd card picked — pick one from the main menu")
    for key in need:
        if not ctx.get(key):
            problems.append(f"{key.replace('_', '.') or key} missing — see setup in the readme")
    for key in need:
        if key == "save3ds" and not ctx.get("save3ds"):
            problems.append(SAVE3DS_HINT)
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
    picks = ui.pick_many("sd card candidates", rows)
    if not picks:
        return
    if picks == [len(rows)]:
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
        "create the venv + pip install custom-install now? (needs internet) — "
        f"python is {sys.executable}", default=True):
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
    "list": flow_list,
    "cifinish": flow_cifinish,
    "inspect": flow_inspect,
    "toolchain": flow_toolchain,
    "check": flow_check,
}


def main_options(_ctx: dict) -> list[tuple[str, str]]:
    return [
        ("install game dump(s) onto the sd card", "install"),
        ("list titles installed on the card", "list"),
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
    """plain-text terminal io вариант, works on pipes + dumb terminals."""

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
            c.init_pair(1, c.COLOR_GREEN, -1)
            c.init_pair(2, c.COLOR_CYAN, -1)
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

    _attr_warned = False

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
