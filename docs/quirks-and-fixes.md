# tool quirks worth knowing

things run into while using the tools this repo drives, and what we do about
them. sorted by which program they belong to.

## save3ds_fuse (wwylele/save3ds)

- **`-x` / `--extract` is a plain flag, not `-x DEST`.** the destination
  directory is the single positional argument — put it last:

  ```
  save3ds_fuse --db sdtitle -x --sd "$SD" -m ~/3ds/movable.sed -b ~/3ds/boot9.bin DEST
  ```

  writing `-x DEST` instead makes save3ds complain with the wonderfully vague
  `Please specify one mount path` (the DEST eats a positional that isn't there).

- **boot9 is required even when "only" reading an SD database.** without
  `-b boot9.bin`, the failure is `Cannot open SD due to missing SD or
  movable.sed` — which sends you down the completely wrong debugging path.
  always pass `-m` AND `-b` (custominstall itself does the same).

- extraction dumps *system* titles too, not just your games. game entries
  stand out through the 128-byte title info entry (a product code like
  `CTR-P-AQEE` in the text field, and entry names that are pure 16-hex-digit
  title ids).

- a stray `-0004000000033500`-looking file can appear in extracted output;
  our scripts just skip non-16-hex names.

## custom-install / custom-install-finalize (ihaveamac)

- **the pc side installs no tickets at all.** `cifinish.bin` only carries
  title ids (plus a seed when one is required); the finalize homebrew app
  builds and installs the ticket itself on the console in ~20 seconds. that's
  also why nothing ticket-shaped needs fixing from the pc.
- `cifinish.bin` is **single-use**: finalize consumes it. whenever a later
  run references a title again, write a fresh file
  (`scripts/make_cifinish.py` or the tui).
- re-running finalize for an already-finalized title is fine — the ticket
  just gets replaced. it's a reasonable first step for a title that shows up
  on HOME but crashes.
- remember the version field: cifinish v3 entries are 0x20 bytes
  (`TITLE\0` + has_seed + pad + u64 tid + 16-byte seed). v2 and real v1
  layouts are different; don't mix them.

## the card itself

- `dbs/title.db` is a proprietary database wrapped in per-console sd crypto
  + cmac — there is no plain file to edit from the pc. read entries through
  `save3ds_fuse --db sdtitle -x` (see above) and do deletions on the console
  with FBI (Titles → Delete; Tickets menu for orphan tickets).

## seeds

- region matters, a lot: us majora's mask 3d (`0004000000125500`) and us
  fates se (`0004000000179800`) are **not** seed-encrypted (checked through
  the ncch `uses_seed` flag), while mm3d twn/kor are. so don't chase seeds
  for titles that don't need them — nothing beats verifying from the actual
  ncch flags for the specific dump you have.

## what one card's lived experience says

- a fresh cifinish (mm3d only) + a second finalize run did **not** stop the
  mm3d launch crash on the card behind these notes. the ladder's remaining
  rungs (fbi reinstall from a re-converted cia sitting at the card root, then
  replacing the dump) are the standing suspects — if you hit the same, don't
  burn hours on seeds first.
