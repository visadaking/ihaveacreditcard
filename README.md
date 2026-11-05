# ihaveacreditcard

installing 3ds games onto an sd card from a linux pc.

it uses:

- [custom-install](https://github.com/ihaveamac/custom-install) by ihaveamac
  (the python package): it writes the game, its contents and the title database
  entries straight onto the sd card
- [save3ds_fuse](https://github.com/wwylele/save3ds), built and on your PATH
  (used for the database work)
- **custom-install-finalize**: the one short console step, ~20 seconds in the
  homebrew launcher, installs the ticket

> the name is a joke on ihaveamac's name. what's inside is real though.

**linux only on the pc side.** the upstream installer also runs on windows and
macos, but every command, path and script here is written around a linux pc.

you need a luma3ds console (hbl or hbmenu reachable). dump your own games and
your own keys; not affiliated with nintendo, obviously.

## easy mode

clone this repo and run:

```
python3 scripts/tui.py
```

it checks your keys, finds mounted sd card candidates, installs dumps and can
write `cifinish.bin` for the finalize step — guided menus all the way, with a
plain numbered-menu fallback (`--plain`) for weird terminals. `install.sh`
bootstraps a fresh linux box (distro packages, save3ds build, venv).

## setup

keys have to come from your own console ([3ds hacks guide][guide] explains
dumping system files). i keep mine in `~/3ds/`:

[guide]: https://wiki.hacks.guide/wiki/3DS:Dump_system_files

```
mkdir -p ~/3ds
# boot9.bin and movable.sed go in there, seeddb.bin if you have one
```

python side (venv, so nothing touches the system python):

```
python3 -m venv ~/.venvs/custom-install
~/.venvs/custom-install/bin/pip install --upgrade \
  https://github.com/ihaveamac/custom-install/archive/refs/heads/python-package.zip
```

build save3ds per its README and drop `save3ds_fuse` somewhere on PATH, e.g.
`~/.local/bin/`. the installer finds boot9/seeddb in `~/3ds/` on its own.

## installing a dump

`$SD` is where the sd card is mounted.

```
~/.venvs/custom-install/bin/custominstall \
  -b ~/3ds/boot9.bin -m ~/3ds/movable.sed -s ~/3ds/seeddb.bin \
  --sd "$SD" \
  "Game (USA).3ds"
```

decrypted trimmed `.3ds` dumps work directly (`.cia` too — 3dsconv does the
conversion for you).

then: eject, homebrew launcher → custom-install-finalize → wait → reboot.
finalize builds the ticket itself — the pc side never needs ticket crypto.

## when a game is on home but crashes on launch

work down this ladder:

1. re-finalize just that one title:

```
python3 scripts/make_cifinish.py -o "$SD/cifinish.bin" 0004000000125500
```

   then hbl → finalize → reboot → test. safe even if the ticket already
   exists, and only the listed titles get touched.

2. FBI reinstall: delete the title in FBI (Titles), then install the `.cia`
   from the card root, test again.

3. still crashing? then the dump itself is the suspect. use another dump.

## checking the pc side before blaming the console

```
rm -rf /tmp/dbcheck
save3ds_fuse --db sdtitle -x --sd "$SD" \
  -m ~/3ds/movable.sed -b ~/3ds/boot9.bin /tmp/dbcheck
python3 scripts/inspect_title_db.py /tmp/dbcheck 0004000000125500
```

things to sanity check against your install log:

- content sizes on the card match the cia/tmd
  (`Nintendo 3DS/<id0>/<id1>/title/00040000/<title low>/content/`)
- `.sav` file sizes match the tmd save size
- the db entry is there, with the content ids / flags you expect

## removing a title

not from the pc. `dbs/title.db` on the card is wrapped in per-console sd
crypto + cmac, there is no plain database file to edit. FBI on the console
does it: Titles → (title) → Delete. if a ticket lingers after deleting the
contents some other way, FBI → Tickets handles the leftovers.

## seeds

keep a seeddb around for 2015+ games, but most of those games don't use seed
crypto at all. checked over here: us mm3d (`0004000000125500`) and us fates se
(`0004000000179800`) are not seed-encrypted, while mm3d twn/kor are. region
matters — don't chase seeds for titles that don't need them.

these tool gotchas (save3ds argument shapes, ticket/cifinish lifecycle, card
db realities) plus the fixes this repo routes around them are written up in
[docs/quirks-and-fixes.md](docs/quirks-and-fixes.md).

## scripts

- `scripts/tui.py` — the guided ui: checks keys, finds mounted sd cards,
  installs dumps, lists titles on the card and writes `cifinish.bin`
  (`--plain` for a numbered menu on dumb terminals / over ssh). on every
  launch it auto-snapshots the card's dbs + `cifinish.bin` into
  `~/Documents/ihaveacreditcard-backups/`, copies the `.sav` files before any
  run that would overwrite saves, and keeps the newest 30 snapshots
- `scripts/doctor.py` — the self-check on its own: python version, key files
  verified by magic bytes + size (not just existence), that save3ds_fuse and
  custominstall actually execute, and that the card is mounted writable.
  `--strict` turns warnings into failures for scripting; the tui's menu has
  the same check, and `python3 scripts/tui.py --check` runs it non-interactively
- `scripts/make_cifinish.py` — non-interactive way to write a fresh
  `cifinish.bin` listing only the titles you pass
- `scripts/inspect_title_db.py` — decodes the 128-byte title entries save3ds
  extracts from the card's title.db (each extracted file is one entry, named
  after the title id)
- `install.sh` — one-shot bootstrap for a fresh linux box: distro packages
  (apt/dnf/pacman/zypper/apk — which also covers non-systemd distros like void
  or artix — plus xbps/emerge, and manual hints on nixos), save3ds build,
  python venv with custom-install (`--dry-run` to preview). zero systemd is
  used anywhere in the toolkit.

## cifinish.bin (v3) format

```
cifinish.bin v3, values little-endian
"CIFINISH"     8 bytes
version        u32 = 3
entry count    u32
--- per entry (0x20 bytes):
"TITLE\0"      6 bytes
has_seed       u8,  0 = no seed
padding        u8 = 0
title id       u64
seed           16 bytes, all zero when has_seed = 0
```

no ticket data in the file — finalize builds the ticket on the console. the
file is single-use: once finalize has eaten it, write a new one for the next
job.

## credits

- custom-install by ihaveamac and contributors (ships the finalize homebrew
  app too)
- save3ds by wwylele
- 3dsconv by ihaveamac (installed as a dependency of custom-install)

all the real functionality is theirs. my part here is the notes, the scripts,
the tui and install.sh — that part is MIT (see [LICENSE](LICENSE)) and applies
to this repo only, not to the upstream projects.

individual shout-outs live in [THANKS.md](THANKS.md).
