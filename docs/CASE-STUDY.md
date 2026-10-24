# Case study: one real incident, with numbers

*(Details that identify the owner's data are left out. Numbers are from the actual run.)*

| | |
|---|---|
| Hardware | Synology DS1821+, DSM 7.2.1-69057 Update 11, kernel 4.4, `btrfs-progs` v4.0 |
| Damaged volume | BTRFS, 36 TiB, 6 HDDs in RAID 5, 17.3 TiB used, 0 device errors, clean scrub; no snapshots, no backups |
| Deleted share | the first subvolume ever created on the volume (id 257), about 6 TiB of live data in about 86,000 entries |
| Cause | uninstall of a community package with "Erase all of the package data files" (see `THE-BUG.md`) |

## Timeline

| When | What |
|---|---|
| t + 0 | package uninstall runs for 45 seconds and reports success |
| t + 1 h 45 | owner notices: share listed in Control Panel, gone from File Station; NFS and a container fail |
| t + 2 h 45 | metadata copy (3.1 GB) safely on another volume |
| t + 4 h | full restore starts (after a successful 47-file canary) |
| t + ~6.5 h | restore of 3.0 TiB expected to finish |

## What the metadata copy contained

* 3 metadata chunks of 1 GiB + the 32 MiB system chunk = 3.1 GB, on a volume with 17 TiB of data.
* **196,635** intact tree blocks (every 16 KiB block verified: fsid, own address, CRC32C).
* Of those, **15,185 leaves + 534 interior nodes** belonged to the deleted subvolume (generations 24 ... 7,759,584).
* The live tree of the deleted subvolume was a single leaf with 17 items and no directory entries: the files had been removed one
  by one, and every record about them was in *older versions* of leaves.

## Rebuilding the tree

* 556,796 distinct items found; **35,170 dropped** as already-deleted before the mass delete (stale ghosts such as old copies of
  folders that had since been replaced).
* 88,106 live inodes, 85,973 entries in the final tree, **zero** name collisions.
* Folders being restored: every file name that survived had a complete extent map.

## Mapping and reuse

* 2,433 data chunks of 10 GiB in the current chunk map. The delete emptied chunks, which BTRFS removed; stale chunk-tree and
  device-tree blocks supplied 532 more, usable, old chunk locations.
* Extents allocated since the delete: 12,466, 0.17 GB in total; **none** overlapped any file in the restore plan.

## Restore

* Plan: 76,338 files, 4,924 directories, 100,251 extent/inline records, 3.02 TiB to read, in four tiers.
* Speed: 300 to 390 MiB/s read from the RAID 5 array, written to a second volume.
* Canary: 47 files (3.9 GB) from both "current" and "removed" chunks: ZIP member CRCs, 7z tests, full image decodes, ffmpeg decodes,
  PDF parsing. 46 passed. The 47th, a video clip, was truncated in the original (`File ended prematurely`).
* Independent check: a container's own database listed every audio file it had ever indexed; all 1,821 were in the plan.

Final validation results: see the blog post / repository description.
