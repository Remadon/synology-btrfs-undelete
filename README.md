# synology-btrfs-undelete

Get files back from a **deleted BTRFS subvolume (for example a Synology shared folder)** by using the metadata that BTRFS
leaves behind, when you have no snapshot or backup.

This toolkit comes from a real incident: an uninstall script on a Synology DS1821+ erased a 3 TB share (about 76,000 files) and
reported success. All of those files were recovered with the tools and process in this repository. The case study is in
[`docs/CASE-STUDY.md`](docs/CASE-STUDY.md); the upstream bug that caused it is described in
[`docs/THE-BUG.md`](docs/THE-BUG.md).

> ### Read this first
> * **Time matters.** The file *data* usually survives for a long time, but the *metadata* (names, folders, where each file's data
>   is) is small and gets recycled by normal writes. Stop writing to the volume, **do not reboot, do not click "Repair", do not
>   recreate the share**, and copy the metadata off the volume first ([`docs/FIRST-HOUR.md`](docs/FIRST-HOUR.md)).
> * Nothing here ever writes to the damaged volume. Even so, **read the code before you run anything as root** on a machine that
>   holds data you care about. It is short on purpose.
> * This is forensic tooling that worked for one incident, not a product. No warranty. See [Limits](#limits).

## What you need

* A BTRFS volume where a subvolume (share) was deleted or emptied, still mounted and running, ideally with SSH access (Synology
  DSM 7 was used; other Linux hosts should work with small changes to the collection script).
* **Another volume** with enough free space to hold the metadata copy (a few GB) and the files you want back.
* A Linux PC with Python 3 (standard library only) and `gcc` to build `scanmeta`.
* Not covered: compressed data (those extents are skipped, never guessed), encrypted shares, hardware failures, volumes whose
  freed space has already been reused.

## The process

Each step is explained in more detail in [`docs/HOW-IT-WORKS.md`](docs/HOW-IT-WORKS.md).

### 0. Stop the bleeding
See [`docs/FIRST-HOUR.md`](docs/FIRST-HOUR.md). Then, on the NAS (SSH), find the id of the deleted subvolume:

```sh
sudo btrfs subvolume list -d /volume2    # "ID 257 ... path DELETED"  -> the deleted share's subvolume id is 257
mount | grep volume2                      # the block device, e.g. /dev/mapper/cachedev_0
df -P /volume1/recovery                   # the device of the volume you are copying TO, e.g. /dev/mapper/cachedev_1
```

### 1. Copy the metadata off the damaged volume (on the NAS, as root)

```sh
sudo DEV=/dev/mapper/cachedev_0 OUT=/volume1/recovery OUTVOL_DEV=/dev/mapper/cachedev_1 DELETED_TREE=257 \
     sh scripts/collect-metadata.sh
```

This reads the damaged volume's block device and writes only under `OUT`, which it insists is on a different volume. Typical size:
a few GB. Then copy `OUT` to your PC (for example `ssh nas "tar -C /volume1/recovery -cf - ." | tar -xf - -C ~/recovery/images`).
**Once this is done, the urgency is mostly over**: the copy is frozen. Keep the NAS running quietly, or leave volume writes to a minimum, because
the *data* is still only safe while its space is not reused (step 6 checks that).

### 2. Build the tools (on your PC)

```sh
make            # builds scanmeta and extent-restore (dynamic)  -  or use bin/extent-restore-x86_64-linux-static for the NAS
```

### 3. Scan: find every intact metadata block

```sh
python3 py/btrfs_undelete.py --images ~/recovery/images --work ~/recovery/work scan --tree 257 --scanmeta ./scanmeta
```

Prints how many intact tree blocks exist and, importantly, a suggested `--before-gen`.

### 4. Rebuild the file tree as it was before the deletion

```sh
python3 py/btrfs_undelete.py --images ~/recovery/images --work ~/recovery/work tree --tree 257 --before-gen N
```

Prints the files, folders and sizes it could reconstruct, by top-level folder. Check that this looks like your data.

### 5. Rebuild the logical-to-physical map

```sh
python3 py/btrfs_undelete.py --images ~/recovery/images --work ~/recovery/work chunks
```

Reports what share of your extents now has a disk location. (When BTRFS empties a data chunk it deletes it; this step recovers
the old mapping from stale chunk-tree and device-tree blocks.)

### 6. Write the restore plan (and check for reused space)

Copy `examples/plan-config.example.json`, choose which top-level folders to skip, and which to restore first:

```sh
python3 py/btrfs_undelete.py --images ~/recovery/images --work ~/recovery/work plan --tree 257 --config my-plan.json
```

This refuses to include any file whose data extent was allocated after the deletion (that space has been reused) and prints
what it skipped and why. The plan is `work/plan.txt`.

### 7. Canary first

Pick ~50 representative files into a tiny plan, restore them, and **validate them with real integrity checks** (ZIP CRCs,
`7z t`, image decoding, `ffmpeg`, `pdfinfo`). If a mapping or offset assumption is wrong for your volume, this is where it shows.

### 8. Restore (on the NAS, as root)

Copy `bin/extent-restore-x86_64-linux-static` (or your own build) and `plan.txt` to the NAS:

```sh
sudo ./extent-restore --dev /dev/mapper/cachedev_0 --dest /volume1/Recovered/Restored --plan plan.txt \
     --guard /volume2 --journal /volume1/Recovered/.restore-journal --log /volume1/Recovered/.restore.log
```

The tool is restartable (run the same command again to resume), writes `*.restore-partial` and renames on completion, sets
owner/mode/mtime, and takes the plan's tiers in order. Run it with `nohup` if you will close your terminal.

### 9. Verify and put the data back

```sh
python3 py/btrfs_undelete.py --images ~/recovery/images --work ~/recovery/work verify --ssh-cmd ssh user@nas --remote-root /volume1/Recovered/Restored
```

Check it yourself too. Only after you are happy: create a fresh share on the damaged volume and copy the data back.
(Writing to the damaged volume earlier can destroy data you have not restored yet.)

## Safety properties

* `collect-metadata.sh`: reads the damaged device; refuses to write anywhere but `OUT` on another volume; no mount/umount/repair.
* `scanmeta`, `btrfs_undelete.py`: work only on the metadata copy.
* `extent-restore`: opens the source with `O_RDONLY` and only `pread()`s it; **requires `--guard`**, refuses if the destination,
  journal or log are under the guard path or on the same filesystem; rejects absolute paths and `..` in the plan.
* Tests: a fake-device suite (sparse files, inline data, empty files, odd names, resume, guards) is in `tests/`.

## Limits

* Tested on one incident: DSM 7.2, a single-device BTRFS volume (the RAID/LVM layer is invisible because the tool reads the
  volume's block device), uncompressed data, data checksums off for the share.
* Compressed extents (zlib/lzo/zstd) are skipped. Encrypted shares are not supported. Inline-only small files are supported.
* A file modified shortly before the deletion can come back in an earlier version if only an older version of its record survived.
* A file whose name record *and* location record were both lost cannot be seen. In the incident, none of the files in the restored
  folders had a name without a location, but the media folder with the most churn lost many records.
* Hard links, ACLs and extended attributes are not restored. Ownership, mode and mtime are.

## Files

| Path | What |
|---|---|
| `scripts/collect-metadata.sh` | NAS side, step 1 |
| `src/scanmeta.c` | finds intact tree blocks (CRC32C, fsid, address) |
| `src/extent-restore.c` | the restore program |
| `py/btrfs_undelete.py` | scan / tree / chunks / plan / verify |
| `bin/` | static x86-64 build of `extent-restore` + `SHA256SUMS` |
| `scripts/postuninst-bug-demo.sh` | safe demonstration of the upstream bug |
| `docs/` | first-hour checklist, how it works, the bug, case study |
| `tests/` | fake-device test of `extent-restore` |

## License

See `LICENSE`.
