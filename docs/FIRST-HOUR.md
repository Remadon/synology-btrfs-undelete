# The first hour: what to do, and what not to

A share (BTRFS subvolume) has been deleted or wiped and you have no snapshot or backup. The clock is running on the **metadata**,
not on the file data: a big volume has only a few GB of metadata, and ordinary writes (containers, databases, indexing)
recycle that space continuously.

## Do

1. **Stop writing to the volume.** Pause downloads, backups, containers. Every write can reuse space you want back.
2. **Enable SSH** (DSM: Control Panel > Terminal & SNMP) and log in as an administrator. Everything below is read-only.
3. **Identify the deleted subvolume.** BTRFS lists deleted-but-not-yet-cleaned subvolumes separately:
   ```sh
   sudo btrfs subvolume list -d /volume2     # "ID 257 gen ... top level 0 path DELETED"
   sudo btrfs subvolume list /volume2        # live subvolumes (your shares)
   mount | grep volume2                       # the block device, e.g. /dev/mapper/cachedev_0
   ```
   The oldest share has the lowest subvolume id. Cross-check with a container bind mount if you have one:
   `grep <share> /proc/<pid>/mountinfo` shows `subvolid=<id>` and a path ending in `//deleted`.
4. **Check for something easier**: Snapshot Replication snapshots, Hyper Backup, another copy, a sync peer, the recycle bin.
5. **Look at the logs for the cause**, without changing anything: `/var/log/synopkg.log`, `/var/log/packages/<pkg>.log`,
   `/var/log/messages`. It matters for knowing whether a cleanup job might still be running.
6. **Copy the metadata off the volume** with `scripts/collect-metadata.sh` (see the README). That freezes the evidence.

## Don't

* **Don't reboot.** BTRFS resumes its background cleanup of the deleted subvolume at mount.
* **Don't click Repair / run `btrfs check --repair` / balance / scrub / defrag / "Space reclamation".**
* **Don't recreate the share** (or anything else) on that volume: new metadata lands in exactly the area you need.
* **Don't write recovered files back onto the damaged volume** until everything you need is safely elsewhere.
* Don't assume a clean exit code means nothing happened: the uninstall that caused my incident reported success.

## Is a cleanup running?

```sh
ps | grep btrfs-cleaner      # the kernel thread that frees a deleted subvolume
cat /proc/diskstats          # sample twice, a few seconds apart; a busy cleaner shows sustained reads
```

If the deleted subvolume's tree is still intact, it may be even easier; in my case the files had been removed one by one before
the subvolume itself went, so the tree was a single empty leaf and the rebuild came entirely from older metadata blocks.
