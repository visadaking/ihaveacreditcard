#!/usr/bin/env python3
"""Build a cifinish.bin for custom-install-finalize.

custom-install writes cifinish.bin so the homebrew custom-install-finalize app
knows which SD-installed titles to finalize (install the ticket, add the seed
if required). The file is single-use input for finalize: once processed, its
job is done.

Use this script to build a *fresh* file listing only certain titles, e.g. to
re-run finalize for a single title without redoing a full install:

    make_cifinish.py -o "$SD/cifinish.bin" 0004000000125500

Then eject the card, run custom-install-finalize from the Homebrew Launcher on
the console, and reboot. Re-finalizing already-finalized titles is safe; only
the listed titles are touched.

Format (cifinish v3):

    offset size value
    0x00   8    ASCII "CIFINISH"
    0x08   4    version, u32 little-endian (= 3)
    0x0C   4    entry count, u32 little-endian
    --- per entry, 0x20 bytes:
    0x00   6    ASCII "TITLE\\0"
    0x06   1    has_seed (0 = none, 1 = seed bytes present)
    0x07   1    padding (0)
    0x08   8    title ID, u64 little-endian
    0x10   16   seed (16 zero bytes when has_seed = 0)

There is no ticket data in the file; finalize constructs the ticket itself on
the console. Only pass --seed for titles that actually require a seed.
"""

from __future__ import annotations

import argparse
import sys

CIFINISH_VERSION = 3


def parse_seed(spec: str) -> tuple[int, bytes]:
    tid_str, sep, seed_hex = spec.partition(":")
    if not sep or not tid_str or not seed_hex:
        raise argparse.ArgumentTypeError(f"expected TID:HEX, got {spec!r}")
    try:
        tid = int(tid_str, 16)
        seed = bytes.fromhex(seed_hex)
    except ValueError as err:
        raise argparse.ArgumentTypeError(str(err)) from None
    if len(seed) != 16:
        raise argparse.ArgumentTypeError("seed must be 32 hex digits (16 bytes)")
    return tid, seed


def build(title_ids: list[int], seeds: dict[int, bytes]) -> bytes:
    header = (
        b"CIFINISH"
        + CIFINISH_VERSION.to_bytes(4, "little")
        + len(title_ids).to_bytes(4, "little")
    )
    entries = bytearray()
    for tid in title_ids:
        seed = seeds.get(tid)
        entries += b"TITLE\0"
        entries += (1 if seed else 0).to_bytes(1, "little")
        entries += b"\0"
        entries += tid.to_bytes(8, "little")
        entries += seed if seed else b"\0" * 0x10
    return header + bytes(entries)


def dump(data: bytes) -> None:
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        asciipart = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        print(f"{off:08x}  {hexpart:<47}  {asciipart}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("title_ids", metavar="TITLE_ID", nargs="+",
                        help="title ID(s), 16 hex digits")
    parser.add_argument("-o", "--out", default="cifinish.bin",
                        help="output path (default: %(default)s)")
    parser.add_argument("--seed", action="append", default=[], metavar="TID:HEX",
                        help="seed for one specific title; repeat per title")
    args = parser.parse_args(argv)

    try:
        title_ids = [int(t, 16) for t in args.title_ids]
    except ValueError:
        parser.error("title IDs must be 16 hex digits")
    for tid in title_ids:
        if not 0 <= tid < 1 << 64:
            parser.error(f"title ID out of range: {args.title_ids[title_ids.index(tid)]}")

    seeds: dict[int, bytes] = {}
    try:
        for spec in args.seed:
            tid, seed = parse_seed(spec)
            seeds[tid] = seed
    except argparse.ArgumentTypeError as err:
        parser.error(str(err))

    data = build(title_ids, seeds)
    with open(args.out, "wb") as f:
        f.write(data)

    print(f"wrote {len(data)} bytes to {args.out}")
    dump(data)
    print("\nnext steps:")
    print(" 1. put this file at the root of the SD card (when not written there directly)")
    print(" 2. insert the card into the 3DS and open the Homebrew Launcher")
    print(" 3. run custom-install-finalize and wait (~20 s)")
    print(" 4. reboot and test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
