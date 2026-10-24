# How it works

This document explains *why* each step exists, using the BTRFS on-disk structures involved. Offsets are for a Linux BTRFS
filesystem with 16 KiB tree blocks (the default; `nodesize` in the superblock). All integers are little-endian.

## 1. Why anything is recoverable

BTRFS is copy-on-write: it never overwrites a metadata block or (normally) a checksummed data block in place. Changing a record
writes a *new* copy of the tree block and later marks the old one free. Deleting a file frees its blocks; it does not erase them.
So after a delete there are two kinds of leftovers:

* **File data** in freed extents. It survives until the allocator hands that space out again.
* **Metadata**: the old tree blocks that described the files. They survive until the metadata allocator reuses them. Metadata is
  tiny compared to data (3 GB for an 18 TB volume here) and is rewritten constantly, so this is the perishable part.

Recovery therefore means: (a) freeze the metadata, (b) rebuild a consistent picture of the file tree from the old tree blocks,
(c) work out where each file's data sits on the device, (d) make sure that space has not been reused, (e) copy the data out.

## 2. Tree blocks you can recognise anywhere

Every tree block (node or leaf) starts with a 101-byte header:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 32 | checksum (CRC32C in the first 4 bytes, over bytes 32..16383) |
| 32 | 16 | `fsid` |
| 48 | 8 | `bytenr` - the block's own *logical* address |
| 80 | 8 | `generation` - the transaction that wrote it |
| 88 | 8 | `owner` - the id of the tree it belongs to |
| 96 | 4 | `nritems` |
| 100 | 1 | `level` (0 = leaf) |

`scanmeta` walks the metadata copy in 4 KiB steps and keeps a block only if `fsid` matches the superblock, `bytenr` equals the
logical address of the place it was found, and the CRC32C verifies. That is a very strong test; random leftovers never pass.
Blocks whose `owner` is the deleted subvolume's id are the old pages of its file tree.

A **leaf** holds 25-byte item headers (`objectid u64, type u8, offset u64, data-offset u32, size u32`), sorted by key, growing
from the front, with the item data growing from the back (data for an item is at `101 + data-offset`).

## 3. What a deleted subvolume looks like

`btrfs subvolume list -d` shows deleted subvolumes whose tree has not been freed yet. In the root tree the subvolume's
`ROOT_ITEM` has `refs 0` and there is an `ORPHAN_ITEM` for its id. How much of the old file tree is still intact depends on *how*
it was deleted:

* `btrfs subvolume delete`: the tree stays intact until the kernel's cleaner thread frees it. If you catch it early, it is worth
  trying `btrfs restore` (run against an unmounted device or an image, with the deleted subvolume's id) before anything
  else. I did not need to, so I can't vouch for it.
* A recursive file delete (what happened to me): every unlink rewrites leaves, so the live tree shrinks to a single empty leaf and
  the old content exists only as freed older versions of leaves. That is the case this toolkit is built for.

## 4. Records that matter

All of these live in leaves of the subvolume's tree:

| Type | Key | Data |
|---|---|---|
| `INODE_ITEM` (1) | `(inode, 1, 0)` | size, owner, mode, times (160 bytes) |
| `INODE_REF` (12) | `(inode, 12, parent-inode)` | `index u64, name_len u16, name` - this file's name in that parent |
| `DIR_INDEX` (96) / `DIR_ITEM` (84) | `(dir-inode, ...)` | child location, type, name |
| `EXTENT_DATA` (108) | `(inode, 108, file-offset)` | inline data, or `disk_bytenr, disk_num_bytes, offset, num_bytes` |

`disk_bytenr` is a *logical* address; the rest of the pipeline turns it into a device offset. Compression and encryption flags are
in the item; compressed extents are skipped by the plan step.

## 4a. Choosing the right version of every record (`btrfs_undelete.py tree`)

Many old versions of each leaf exist (every change writes a new one), and the oldest are usually older than the files you care
about. The rule used:

> For each item key *K*, the **authoritative version** is the one in the **newest surviving leaf with generation <= T that
> covers *K*'s key range**. If that leaf does not contain *K*, the item had already been deleted before the mass delete began,
> and it is dropped (a "ghost").

`T` ("`--before-gen`") is a generation just before the mass delete started. The subvolume's `ROOT_ITEM` has `ctransid`, the last
generation in which anything in the tree changed; `ctransid - 500` worked here. A leaf covers *K* if its first key <= *K* <= its last
key, and a leaf contains every item in its range, so absence from a covering leaf is meaningful.

Then names: the parent comes from a live `INODE_REF` confirmed by a live directory entry; if two siblings claim the same name the
inode with the highest number (created last) wins and the other, with everything below it, is dropped. Children whose parent
directory record is lost are re-homed under `_lost+found/`.

## 5. Logical to physical (`btrfs_undelete.py chunks`)

Logical addresses map to device offsets through the **chunk tree** (`CHUNK_ITEM`: logical start, length, type, stripes with device
offsets). Two things make this the trickiest step after a mass delete:

* BTRFS **deletes data chunks as soon as they become empty**, so exactly the chunks that held your deleted files vanish from the
  current chunk map. (On my volume the chunk tree changed a few transactions after the delete.)
* But the old chunk-tree leaves, and the **device tree** (`DEV_EXTENT`: physical start, length, owning chunk's logical start), are
  copy-on-write too. Their stale versions are in the metadata copy and still describe the deleted chunks.

The tool merges the current list with every `CHUNK_ITEM` and `DEV_EXTENT` it can still find, discards any candidate whose physical
range overlaps a *current* chunk (reused), and refuses to guess when two equally recent candidates disagree.

## 6. Has the space been reused? (`plan`)

Every data extent that is allocated has an `EXTENT_ITEM` in the extent tree whose `generation` is the transaction that created it.
Collecting every `EXTENT_ITEM` with `generation > ctransid` from *all* surviving extent-tree leaves (not only the current tree,
which in a copy taken from a live volume is not perfectly consistent) gives every data extent allocated since the deletion. A file
is only planned if none of its extents overlaps one of those. In my incident that was 12,466 extents totalling 0.17 GB, none of
which touched anything I restored.

Also: no new *chunks* may have been allocated since the delete (the device tree's newest generation tells you), otherwise the
physical range of a deleted chunk could have been handed to a new chunk.

## 7. The restore plan and `extent-restore`

The plan is a tab-separated text file (`PLAN1`): `D` directories, `F` files (id, size, mode, uid, gid, mtime, hex path),
`E` extents (file id, file offset, device offset, length) and `I` inline data. `E` records are written in **device order within
each tier**, so the disks read mostly sequentially, and merged when file- and device-contiguous. `extent-restore` reads each
extent with `pread()` from the device, writes it with `pwrite()` into `<path>.restore-partial`, and when a file's last piece is
done sets owner/mode/mtime and renames it to the final name. A journal makes it restartable.

## 8. What can still go wrong

* Leaves of the live tree that were freed *last* are the first to be overwritten, so some records are gone for good.
* The newest version of a leaf may be gone while an older one survives: a file edited shortly before the delete can come back in
  its older state.
* Data checksums: if the share used them, stale checksum-tree leaves may exist and could be used to verify extents; this
  toolkit does not do that yet.
* BTRFS on top of SSD cache, encryption (eCryptfs shares) and compression are not handled.
