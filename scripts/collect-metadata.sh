#!/bin/sh
# collect-metadata.sh - run as root ON THE NAS (Synology DSM 7, busybox sh) right after a BTRFS subvolume/share was deleted.
#
# PURPOSE: freeze a copy of the damaged volume's btrfs METADATA before it is overwritten, and collect dumps/logs that help the
# analysis. The metadata of even a huge volume is small (mine: 3 GB for 18 TB) but it is recycled continuously by normal writes.
#
# WHAT IT DOES
#   READS : the volume's block device (default /dev/mapper/cachedev_0 = volume2 on DSM): btrfs-debug-tree dumps (retried, because
#           the old btrfs-progs shipped by Synology races with the live filesystem), dd reads of the METADATA and SYSTEM chunks and
#           the superblock areas. Also copies some log files and DSM share configuration.
#   WRITES: only under $OUT, which MUST be on a DIFFERENT volume (it refuses otherwise): /volume1/recovery by default.
# WHAT IT DOES NOT DO: no mount, umount, remount, repair, check, balance, scrub, delete, kill or service stop.
#
# USAGE (as root):   DEV=/dev/mapper/cachedev_0 OUT=/volume1/recovery DELETED_TREE=257 OUTVOL_DEV=/dev/mapper/cachedev_1 sh collect-metadata.sh
#   DEV          block device of the DAMAGED volume (find it with: mount | grep volume2)
#   OUT          destination directory on ANOTHER volume
#   OUTVOL_DEV   block device that OUT lives on (checked to be different from DEV); find with: df -P /volume1/recovery
#   DELETED_TREE subvolume id of the deleted share (btrfs subvolume list -d /volume2) - its root item is dumped for later steps

DEV=${DEV:-/dev/mapper/cachedev_0}
OUT=${OUT:-/volume1/recovery}
DELETED_TREE=${DELETED_TREE:-}
OUTVOL_DEV=${OUTVOL_DEV:-}
DUMP=${DUMP:-btrfs-debug-tree}
PATH=$PATH:/usr/local/bin:/usr/syno/bin:/usr/syno/sbin:/sbin:/usr/sbin:/bin:/usr/bin
export PATH

die() { echo "ABORT: $*"; exit 1; }

[ "$(id -u)" = "0" ] || [ -n "${TEST:-}" ] || die "must run as root (use sudo)"
case "$OUT" in
  /tmp/*) [ -n "${TEST:-}" ] || die "OUT=$OUT is under /tmp (RAM or the system partition) - choose a directory on another data volume" ;;
  /*) ;;
  *) die "OUT must be an absolute path" ;;
esac
[ -b "$DEV" ] || [ -n "${TEST:-}" ] || die "$DEV is not a block device"
mkdir -p "$OUT" || die "cannot create $OUT"
cd "$OUT" || die "cannot cd to $OUT"
if [ -z "${TEST:-}" ]; then
  OUTDEV=$(df -P "$OUT" | awk "NR==2 {print \$1}")
  [ -n "$OUTVOL_DEV" ] || die "set OUTVOL_DEV to the device that $OUT is on (it is: $OUTDEV) so the script can prove it is not the damaged volume"
  [ "$OUTDEV" = "$OUTVOL_DEV" ] || die "$OUT is on $OUTDEV, not on OUTVOL_DEV=$OUTVOL_DEV"
  [ "$OUTDEV" != "$DEV" ] || die "OUT is on the damaged volume ($DEV) - refusing"
fi

# awk program that turns a btrfs-debug-tree chunk-tree dump into one line per stripe:
#   <logical> <length> <type> <stripe-index> <devid> <device-offset>
# It accepts the type as a name (METADATA|DUP) or as a number (36), and both 1-line and 2-line layouts.
cat > parse-chunks.awk <<'AWKEOF'
function tname(n,   s) {
  s = ""
  if (int(n) % 2 == 1) s = s "DATA"
  if (int(n / 2) % 2 == 1) s = s (s == "" ? "" : "_") "SYSTEM"
  if (int(n / 4) % 2 == 1) s = s (s == "" ? "" : "_") "METADATA"
  if (int(n / 8) % 2 == 1) s = s "_RAID0"
  if (int(n / 16) % 2 == 1) s = s "_RAID1"
  if (int(n / 32) % 2 == 1) s = s "_DUP"
  if (s == "") s = "UNKNOWN" n
  return s
}
/CHUNK_ITEM/ && /key \(/ {
  if (match($0, /CHUNK_ITEM [0-9]+/)) logical = substr($0, RSTART + 11, RLENGTH - 11)
}
{
  for (i = 1; i < NF; i++) {
    if ($i == "length" && i > 1 && $(i-1) == "chunk") len = $(i+1)
    if ($i == "type") {
      v = $(i+1)
      if (v ~ /^[0-9]+$/) type = tname(v + 0)
      else if (v ~ /^(DATA|METADATA|SYSTEM)/) { type = v; gsub(/\|/, "_", type) }
    }
  }
}
/stripe [0-9]+ devid/ { print logical, len, type, $2, $4, $6 }
AWKEOF

# The old btrfs-progs opens the LIVE filesystem and fails whenever a commit lands mid-open
# ("parent transid verify failed", "unable to open"), so every dump is retried.
get_chunks() {
  n=0
  while [ "$n" -lt 20 ]; do
    n=$((n + 1))
    # method 1: print only the chunk tree
    $DUMP -t 3 "$DEV" > chunk-tree.txt 2> chunk-tree.err < /dev/null
    if grep -q CHUNK_ITEM chunk-tree.txt; then
      echo "  chunk tree read with -t 3 on attempt $n"; return 0
    fi
    # method 2: default dump (root tree, then chunk tree, then others); keep only the chunk-tree part
    $DUMP "$DEV" 2> chunk-tree.err < /dev/null | awk "
      /CHUNK_ITEM/ && /key \\(/ { on = 1 }
      on && / tree key \\(/ { exit }
      on { print }" > chunk-tree.txt
    if grep -q CHUNK_ITEM chunk-tree.txt; then
      echo "  chunk tree read from the default dump on attempt $n"; return 0
    fi
    sleep 1
  done
  return 1
}

# dump_retry <tree-id> <outfile> <min-lines>
dump_retry() {
  n=0
  while [ "$n" -lt 12 ]; do
    n=$((n + 1))
    $DUMP -t "$1" "$DEV" > "$2" 2> "$2.err" < /dev/null
    if [ "$(wc -l < "$2")" -ge "$3" ]; then echo "  tree $1 -> $2 ($(wc -l < "$2") lines, attempt $n)"; return 0; fi
    sleep 1
  done
  echo "  tree $1: could not dump after $n attempts (see $2.err)"
  return 1
}

main() {
  echo "start: $(date)"
  echo "host: $(hostname)  device: $DEV  out: $OUT"
  df -h "$OUT" | tail -n 1

  echo
  echo "--- 1/5 find volume2's metadata chunks (read-only, retried)"
  if get_chunks; then
    awk -f parse-chunks.awk chunk-tree.txt > chunks.txt
    echo "  stripe rows parsed: $(wc -l < chunks.txt)"
    awk "{ c[\$3]++; b[\$3] += \$2 } END { for (t in c) printf \"    %-18s stripes=%d logical_bytes=%.0f\n\", t, c[t], b[t] }" chunks.txt
    awk "\$3 ~ /METADATA|SYSTEM/ && \$4 == 0" chunks.txt > meta-chunks.txt
    total=$(awk "{ s += \$2 } END { printf \"%.0f\", s }" meta-chunks.txt)
    echo "  METADATA+SYSTEM chunks to copy (first stripe of each): $(wc -l < meta-chunks.txt), $total bytes"
  else
    echo "  FAILED to read the chunk tree. First lines of what the tool printed:"
    head -n 30 chunk-tree.txt
    head -n 12 chunk-tree.err
    : > chunks.txt; : > meta-chunks.txt; total=0
  fi

  echo
  echo "--- 2/5 copy METADATA and SYSTEM chunks (dd reads of $DEV)"
  : > manifest.txt
  if [ "$total" -gt 0 ] && [ "$total" -lt 17179869184 ]; then
    avail_kb=$(df -Pk "$OUT" | awk "NR==2 {print \$4}")
    need_kb=$(( total / 1024 + 4194304 ))
    if [ "$avail_kb" -gt "$need_kb" ]; then
      while read -r logical len type idx devid off; do
        bs=1048576
        if [ $((off % bs)) -ne 0 ] || [ $((len % bs)) -ne 0 ]; then bs=4096; fi
        f="chunk-$logical-$type.img"
        echo "  logical=$logical len=$len type=$type dev_offset=$off -> $f"
        dd if="$DEV" of="$f" bs=$bs skip=$((off / bs)) count=$((len / bs)) 2>> dd.log
        echo "$logical $len $type $off $f" >> manifest.txt
      done < meta-chunks.txt
    else
      echo "  NOT COPIED: not enough free space on $OUT ($avail_kb KB free, need $need_kb KB)"
    fi
  else
    echo "  NOT COPIED: no usable chunk list (total=$total bytes)"
  fi
  dd if="$DEV" of=dev-first-1MiB.img bs=1048576 count=1 2>> dd.log
  dd if="$DEV" of=dev-mirror-64MiB.img bs=1048576 skip=64 count=1 2>> dd.log
  dd if="$DEV" of=dev-mirror-256GiB.img bs=1048576 skip=262144 count=1 2>> dd.log
  blockdev --getsize64 "$DEV" > dev-size.txt 2>&1

  echo
  echo "--- 3/5 logs and DSM share config (copies)"
  mkdir -p logs
  for f in /var/log/messages /var/log/synopkg.log /var/log/synoscgi.log /var/log/rm.log \
           /var/log/bash_history.log /var/log/bash_err.log /var/log/auth.log /var/log/synopkgmgr.log \
           /var/log/synoinstall.log /var/log/synoplugin.log /var/log/synoschedmulti.log \
           /var/log/synoscheduler.log /var/log/syslog.log /var/log/systemd.log \
           /etc/samba/smb.share.conf /etc/exports /usr/syno/etc/synoshare.db; do
    if [ -f "$f" ]; then cp -p "$f" "logs/$(echo "$f" | tr "/" "_")"; fi
  done
  cp -rp /var/log/synolog logs/synolog 2> /dev/null
  cp -rp /usr/syno/etc/synoshare.bak logs/synoshare.bak 2> /dev/null
  ls -la logs | head -n 40

  echo
  echo "--- 4/5 tree dumps (read-only, retried)"
  dump_retry 1 root-tree.txt 50
  if [ -n "$DELETED_TREE" ]; then dump_retry "$DELETED_TREE" deleted-tree-now.txt 10; fi
  btrfs inspect-internal dump-super -f "$DEV" > super.txt 2>&1 < /dev/null
  echo "  super.txt lines: $(wc -l < super.txt)"

  echo
  echo "--- 5/5 checksums and permissions"
  md5sum chunk-*.img dev-*.img > md5.txt 2> /dev/null
  chmod -R a+rX "$OUT"
  ls -la "$OUT"
  du -sh "$OUT"
  echo "finished: $(date)"
  echo "==== IMAGE DONE ===="
}

main 2>&1 | tee -a "$OUT/run.log"
