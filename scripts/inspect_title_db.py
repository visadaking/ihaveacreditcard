#!/usr/bin/env python3
"""decode 128-byte title entries extracted from a 3ds sd title database.

the sd card's ``Nintendo 3DS/<id0>/<id1>/dbs/title.db`` lists every
sd-installed title, one 128-byte "title info entry" per title. save3ds can
extract those entries for inspection (each output file is one entry, named
after the title id):

    save3ds_fuse --db sdtitle -x --sd "$SD" \\
        -m ~/3ds/movable.sed -b ~/3ds/boot9.bin /tmp/dbcheck

then run this script:

    inspect_title_db.py /tmp/dbcheck                  # every entry
    inspect_title_db.py /tmp/dbcheck 0004000000125500 # one specific title

field notes (reverse-engineered by cross-referencing pyctr's title database
code and comparing entries of known-good vs. misbehaving installs):

- ``size_on_sd`` should grow with the game's content size (plus save space),
- ``flags0`` bit ``0x1`` was set exactly on the titles that ship a *manual*
  cfa (cross-check only),
- ``extdata_id_low`` being non-zero means the game expects system extdata.

treat it as a verification aid, not a complete schema.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

# layout of the first 0x30 bytes of a title info entry (entries are 128 bytes)
_ENTRY = struct.Struct("<QIHHIIIIIIQ")


def decode_entry(entry: bytes) -> dict:
    """decode one 128-byte title info entry into a plain-fields dict."""
    if len(entry) != 128:
        raise ValueError(f"unexpected size {len(entry)} (expected 128)")
    (size, itype, version, ncchver, flags0, tmdid, cmdid, flags1,
     extdataid_low, reserved, flags2) = _ENTRY.unpack_from(entry, 0)
    return {
        "size_on_sd": size,
        "type": itype,
        "version": version,
        "ncch_version": ncchver,
        "flags0": flags0,
        "tmd_content_id": tmdid,
        "cmd_content_id": cmdid,
        "flags1": flags1,
        "extdata_id_low": extdataid_low,
        "reserved2": reserved,
        "flags2": flags2,
        "code_text": entry[0x30:0x40].rstrip(b"\0").decode("ascii", "replace"),
        "extra": struct.unpack_from("<I", entry, 0x50)[0],
        "tail_all_zero": entry[0x54:] == bytes(128 - 0x54),
    }


def entry_lines(tid: str, d: dict) -> list[str]:
    lines = [
        f"{tid}:",
        f"  size_on_sd     : {d['size_on_sd']:#x} ({d['size_on_sd']} bytes)",
        f"  type           : {d['type']:#x}",
        f"  version        : {d['version']:#06x}",
        f"  ncch_version   : {d['ncch_version']}",
        f"  flags0         : {d['flags0']:#x}  (bit 0x1: manual cfa present, hint)",
        f"  tmd_content_id : {d['tmd_content_id']:#x}",
        f"  cmd_content_id : {d['cmd_content_id']:#x}",
        f"  flags1         : {d['flags1']:#x}",
        f"  extdata_id_low : {d['extdata_id_low']:#010x}",
        f"  reserved2      : {d['reserved2']:#010x}",
        f"  flags2         : {d['flags2']:#x}",
        f"  code_text      : {d['code_text']!r}",
        f"  extra@0x50     : {d['extra']:#010x}",
        f"  tail_all_zero  : {d['tail_all_zero']}",
    ]
    return lines


def decode(path: str) -> None:
    with open(path, "rb") as f:
        entry = f.read()
    try:
        d = decode_entry(entry)
    except ValueError as err:
        print(f"{path}: {err}", file=sys.stderr)
        return
    tid = os.path.basename(path)
    print("\n".join(entry_lines(tid, d)))
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("extract_dir",
                        help="directory produced by save3ds_fuse --db sdtitle -x")
    parser.add_argument("title_ids", nargs="*", metavar="TITLE_ID",
                        help="optional title id(s); omit to inspect every entry")
    args = parser.parse_args(argv)

    if args.title_ids:
        names = [t.lower() for t in args.title_ids]
    else:
        names = sorted(n for n in os.listdir(args.extract_dir) if not n.startswith("."))
    if not names:
        print(f"no entries found in {args.extract_dir}", file=sys.stderr)
        return 1

    missing = False
    for name in names:
        path = os.path.join(args.extract_dir, name)
        if not os.path.isfile(path):
            print(f"not found: {path}", file=sys.stderr)
            missing = True
            continue
        decode(path)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
