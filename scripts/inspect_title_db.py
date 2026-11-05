#!/usr/bin/env python3
"""Decode 128-byte title entries extracted from a 3DS SD title database.

The SD card's ``Nintendo 3DS/<id0>/<id1>/dbs/title.db`` lists every
SD-installed title, one 128-byte "title info entry" per title. save3ds can
extract those entries for inspection (each output file is one entry, named
after the title ID):

    save3ds_fuse --db sdtitle -x /tmp/dbcheck \\
        -m ~/3ds/movable.sed -b ~/3ds/boot9.bin "$SD"

Then run this script:

    inspect_title_db.py /tmp/dbcheck                  # every entry
    inspect_title_db.py /tmp/dbcheck 0004000000125500 # one specific title

Field notes (reverse-engineered by cross-referencing pyctr's title database
code and comparing entries of known-good vs. misbehaving installs):

- ``size_on_sd`` should grow with the game's content size (plus save space),
- ``flags0`` bit ``0x1`` was set exactly on titles that ship a *manual* CFA
  (useful as a cross-check only),
- ``extdata_id_low`` being non-zero means the game expects system extdata.

Treat it as a verification aid, not a complete schema.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

# layout of the first 0x30 bytes of a title info entry (entries are 128 bytes)
_ENTRY = struct.Struct("<QIHHIIIIIIQ")


def decode(path: str) -> None:
    with open(path, "rb") as f:
        entry = f.read()
    if len(entry) != 128:
        print(f"{path}: unexpected size {len(entry)} (expected 128)", file=sys.stderr)
        return

    (size, itype, version, ncchver, flags0, tmdid, cmdid, flags1,
     extdataid_low, reserved, flags2) = _ENTRY.unpack_from(entry, 0)

    code = entry[0x30:0x40].rstrip(b"\0").decode("ascii", "replace")
    extra = struct.unpack_from("<I", entry, 0x50)[0]
    tail_all_zero = entry[0x54:] == bytes(128 - 0x54)

    print(f"{path}:")
    print(f"  size_on_sd     : {size:#x} ({size} bytes)")
    print(f"  type           : {itype:#x}")
    print(f"  version        : {version:#06x}")
    print(f"  ncch_version   : {ncchver}")
    print(f"  flags0         : {flags0:#x}  (bit 0x1: manual CFA present, hint)")
    print(f"  tmd_content_id : {tmdid:#x}")
    print(f"  cmd_content_id : {cmdid:#x}")
    print(f"  flags1         : {flags1:#x}")
    print(f"  extdata_id_low : {extdataid_low:#010x}")
    print(f"  reserved2      : {reserved:#010x}")
    print(f"  flags2         : {flags2:#x}")
    print(f"  code_text      : {code!r}")
    print(f"  extra@0x50     : {extra:#010x}")
    print(f"  tail_all_zero  : {tail_all_zero}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("extract_dir",
                        help="directory produced by save3ds_fuse --db sdtitle -x")
    parser.add_argument("title_ids", nargs="*", metavar="TITLE_ID",
                        help="optional title ID(s); omit to inspect every entry")
    args = parser.parse_args(argv)

    if args.title_ids:
        names = [t.lower() for t in args.title_ids]
    else:
        names = sorted(n for n in os.listdir(args.extract_dir))

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
