# ihaveacreditcard · 3DS custom-install toolkit

> The repo name is a joke riff on **ihaveamac** (author of custom-install).
> What's inside is a serious, small toolkit: PC-side helpers + field notes for
> installing retail 3DS game dumps onto a 3DS SD card.

**Scope: Linux only.** The upstream installer happens to run on Windows/macOS
too, but every command, path and script in this repo is written around a Linux
PC (venv, `~/.local/bin`, udisks-style SD mount points, building save3ds).

PC-side helpers and notes for installing retail 3DS game dumps (`.3ds` / `.cia`)
onto a 3DS SD card from a Linux PC, using:

- [custom-install](https://github.com/ihaveamac/custom-install) (Python package,
  `python-package` branch) — writes the title, its contents and the
  `dbs/title.db` entries directly to the SD card,
- [save3ds_fuse](https://github.com/wwylele/save3ds) — for database work,
- **custom-install-finalize** (homebrew app, shipped in the same repo) — the one
  short console step (~20 seconds) that installs the ticket.

Requires a Luma3DS console with access to the Homebrew Launcher / Homebrew Menu.

> Unofficial personal notes/scripts — not affiliated with or endorsed by
> Nintendo. The upstream projects linked below do the actual heavy lifting.

## Contents

| Script | Purpose |
| --- | --- |
| `scripts/make_cifinish.py` | Build a fresh `cifinish.bin` so finalize (re)installs only the titles you list — e.g. re-run finalize for one title without touching anything else |
| `scripts/inspect_title_db.py` | Decode title entries extracted from the SD `dbs/title.db`, for verifying an install purely PC-side |

## Setup (Linux)

Keys must come from your own console (see the
[3DS Hacks Guide](https://wiki.hacks.guide/wiki/3DS:Dump_system_files) for
`boot9.bin`, `movable.sed`, and optional `seeddb.bin`):

```sh
mkdir -p ~/3ds
# place boot9.bin, movable.sed and (optionally) seeddb.bin in ~/3ds/
```

Install the installer into a venv so nothing touches system Python:

```sh
python3 -m venv ~/.venvs/custom-install
~/.venvs/custom-install/bin/pip install --upgrade \
  https://github.com/ihaveamac/custom-install/archive/refs/heads/python-package.zip
```

Build [`save3ds`](https://github.com/wwylele/save3ds) per its README and put
`save3ds_fuse` on `PATH` (e.g. `~/.local/bin/`). The installer also looks for
`boot9.bin` / `seeddb.bin` in `~/3ds/` by default.

## Installing a game dump

`$SD` below means the mount point of the (FAT32) 3DS SD card.

```sh
~/.venvs/custom-install/bin/custominstall \
  -b ~/3ds/boot9.bin \
  -m ~/3ds/movable.sed \
  -s ~/3ds/seeddb.bin \
  --sd "$SD" \
  "Game (USA).3ds"
```

- Decrypted, trimmed `.3ds` dumps work directly (conversion happens via the
  bundled 3dsconv; `.cia` inputs are also accepted).
- Then eject the card, and on the console: Homebrew Launcher →
  **custom-install-finalize** → wait → reboot. That single step installs the
  ticket (finalize *generates* the ticket itself — the PC side never needs
  ticket cryptography) plus the seed, if one is required for that title.

## Re-finalizing a single title

If a title shows up on HOME but misbehaves, re-run finalize for just that
title. Build a minimal `cifinish.bin` and put it at the card root:

```sh
python3 scripts/make_cifinish.py -o "$SD/cifinish.bin" 0004000000125500
```

Then eject → Homebrew Launcher → **custom-install-finalize** → reboot → test.
It is safe to re-finalize a title whose ticket was already installed; finalize
replaces it, and only the titles listed in the file are touched.

## Verifying an install PC-side

Extract the title entries from the card's title database with save3ds (run
while the card is mounted):

```sh
rm -rf /tmp/dbcheck
save3ds_fuse --db sdtitle -x /tmp/dbcheck \
  -m ~/3ds/movable.sed -b ~/3ds/boot9.bin "$SD"
python3 scripts/inspect_title_db.py /tmp/dbcheck 0004000000125500
```

Things to sanity-check against the saved install log:

- the content sizes on the card match the CIA/TMD
  (`Nintendo 3DS/<id0>/<id1>/title/00040000/<title low>/content/*.app`),
- `.sav` file sizes match the TMD save size,
- the DB entry exists with the expected content ids / flags.

## Removing an installed title

This is **not** doable from the PC: `dbs/title.db` on the SD card is a
proprietary database wrapped in per-console SD crypto + CMAC — there is no
plain database file to edit. On the console with FBI:

- **Titles → (title) → Delete** removes the title,
- if a leftover ticket remains (e.g. title contents were deleted another way),
  **FBI → Tickets** can remove the orphan ticket.

## Troubleshooting notes

- **Title appears on HOME but crashes to an ARM11 exception on launch.** Work
  down this ladder:
  1. Re-finalize just that title (see above).
  2. FBI reinstall: delete the title in FBI *Titles*, then install the
     converted `.cia` from the card root in FBI (SD), and test again.
  3. If it still crashes, the dump itself is likely bad — re-dump or source a
     different one.
- **Seeds.** Keep a SeedDB around and reasonably fresh for 2015+ games, but
  note that *most* games of that era do not use seed crypto at all. Verified
  from the NCCH flags (`uses_seed`) over several titles: US original dumps of
  Majora's Mask 3D (`0004000000125500`) and Fire Emblem Fates: Special Edition
  (`0004000000179800`) are **not** seed-encrypted, while Majora's Mask 3D
  TWN/KOR *are* — region matters, so don't chase seeds for titles that don't
  need them.

## cifinish.bin format reference (version 3)

```
offset size value
0x00   8    ASCII "CIFINISH"
0x08   4    version, u32 little-endian (3)
0x0C   4    entry count, u32 little-endian
-- per entry, 0x20 bytes:
0x00   6    ASCII "TITLE\0"
0x06   1    has_seed (0 = none, 1 = seed bytes present)
0x07   1    padding (0)
0x08   8    title ID, u64 little-endian
0x10   16   seed (16 zero bytes when has_seed = 0)
```

There is no ticket data in the file — finalize builds the ticket on the
console. Treat `cifinish.bin` as single-use: once finalize has consumed it,
build a fresh file for any follow-up work.

## Credits / upstream

- [custom-install](https://github.com/ihaveamac/custom-install) by ihaveamac
  and contributors (includes the `custom-install-finalize` homebrew app).
- [save3ds](https://github.com/wwylele/save3ds) by wwylele (database/FUSE
  tooling).
- [3dsconv](https://github.com/ihaveamac/3dsconv) (installed as a dependency of
  custom-install).

These notes were written during hands-on use of the above tools; all the real
functionality is theirs. This repo only documents the procedures and adds the
two small scripts.

## License

MIT — see [LICENSE](LICENSE).
