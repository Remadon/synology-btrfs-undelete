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
| t + 7 h 16 min | restore finished: 76,338 files, 0 failed, 0 read errors |
| t + ~8 h | owner's spot checks of files in every top-level folder (read-only NFS mount): all intact |

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

* Plan: 76,338 files, 4,924 directories, 100,251 extent/inline records, in four tiers. The files add up to 3.06 TiB; 3.02 TiB
  (3,095.6 GiB) had to be read, because 75 of the files contain holes that were already unwritten in the originals (34 GiB in total).
* Speed: typically 240 to 390 MiB/s read from the RAID 5 array and written to a second volume; 271 MiB/s on average
  (195 minutes in all), slowest on the last tier of many small files.
* Canary: 47 files (3.9 GB) from both "current" and "removed" chunks: ZIP member CRCs, 7z tests, full image decodes, ffmpeg decodes,
  PDF parsing. 46 passed. The 47th, a video clip, was truncated in the original (`File ended prematurely`).
* Independent check: a container's own database listed 1,821 library files (audio and ebooks); all of them were in the plan.

## Result

* The restore finished in 195 minutes: **76,338 of 76,338 files, 0 failed, 0 read errors.** A listing taken as root afterwards
  matched the plan exactly (every file present at its planned size, 3,360,536,180,733 bytes in all, no `.restore-partial` leftovers).
* The owner opened files from every top-level folder over a read-only NFS mount and found them intact.
* Our own content checks, on finished files while the restore was still running: a sample of 127 files (ZIP member CRCs, `7z t`,
  image decodes, `ffmpeg` decodes, PDF parsing) gave 124 passes. Of the other three, one was a false alarm in the checker and two
  were incomplete downloads that already had holes in the originals.
* Audiobooks: all 1,841 files in the audiobook folder were present at their exact planned sizes, and twelve of them decoded end to
  end without errors. A container (Audiobookshelf) kept its own database of the library: 1,821 files, 1,803 of them audio and 18
  ebooks. All 1,821 were present, and 1,566 had exactly the size the database recorded. The other 255, all audio, were larger than
  the database's figure. Those recorded sizes are multiples of 4096 (254 of the 255) and nearly all of the files were added in the
  weeks before the incident, which fits a library scan that ran while they were still being copied in. All 255 open cleanly, and
  254 also decode their last 30 seconds without a message. The remaining one decodes completely too, but its embedded cover-art
  atom holds a valid image followed by 8 MB of other bytes. Those bytes start in the middle of a sector, right after the image,
  which points to the original file rather than to a wrongly mapped extent.
* The published static binary (`bin/extent-restore-x86_64-linux-static`) was re-run on the 47-file canary after the main restore
  and produced output identical to the first run: same bytes, owner, mode and modification time.

**What was not, and could not be, checked:** the share had data checksums turned off, so there is nothing to compare restored data
against cryptographically. Correctness rests on the structure of the recovered metadata (each file's extents come from its own
records), the reuse check above, and the content checks listed here. A file that was modified shortly before the deletion could have
come back in an older version.
