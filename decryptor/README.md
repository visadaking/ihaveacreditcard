# decryptor (vendored)

this folder is a vendored copy of [rcyggdra/decryptor][src] (3DS CIA/CCI batch
decryptor), extended here with:

- **`ctrdecrypt/`** — a library + thin CLI that decrypts/probes CIA and CCI
  files **in-process** (no external tools needed for the crypto itself),
  with a progress callback
- **`tui/`** — `decryptor-tui`, a linux-native batch decryptor TUI
  (ratatui) that decrypts, dumps the NCCH parts and repacks them into
  decrypted `.cci`/`.cia` files via [makerom][pctr]

[src]: https://github.com/rcyggdra/decryptor
[pctr]: https://github.com/3DSGuy/Project_CTR

upstream's windows-era scripts (.bat helpers, dlchelper) are not carried over —
everything here builds and runs on linux. the original MIT license applies
(see [LICENSE](LICENSE), Copyright rcyggdra); the additions are under MIT too
(ihaveacreditcard's LICENSE covers them).

## build

```
cargo install --path decryptor/tui      # decryptor-tui + uses ./ctrdecrypt
cargo install --path decryptor/ctrdecrypt  # ctrdecrypt libre CLI
```

rust/cargo alone is enough (the TLS stack is rustls, no libssl needed).
`install.sh` at the repo root does this for you, plus builds makerom+ctrtool
(decryptor-tui calls makerom for the repack step; finds it on PATH or in
`~/.local/bin`).

## decryptor-tui usage

```
decryptor-tui [options] [DIR]     # DIR of .cia/.3ds/.cci/.3dz files (default .)
```

- `--headless` — no interface, script friendly (this is what tui.py drives)
- `--seeddb PATH` — seeddb.bin path (auto-searched in cwd/`~/3ds`/`~/.3ds`)
- `--makerom PATH` — makerom path (auto: PATH, `~/.local/bin`)
- `--no-online` — don't fetch missing seeds from nintendo's servers
- `--keep-intermediates` — keep the dumped .ncch parts after repacking

originals are never written to; outputs are
`<stem>-decrypted.cci` / `<stem>-decrypted.cia` (plus
`<stem> (Update)-decrypted.cia` / `<stem> (DLC)-decrypted.cia` when the
dump has update/DLC partitions). already-decrypted dumps are skipped with a
note, not re-processed.

## ctrdecrypt usage

```
ctrdecrypt [options] ROMFILE     # same probe + decrypt, plain output
```

run `ctrdecrypt --help` for the options; the library (`ctrdecrypt` crate)
exposes `decrypt(path, &DecryptOptions)` + `probe(path)` for embedding.
