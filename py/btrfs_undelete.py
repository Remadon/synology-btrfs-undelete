#!/usr/bin/env python3
"""btrfs_undelete.py - rebuild the files of a deleted BTRFS subvolume (e.g. a Synology shared folder) from the metadata
that is still lying around, and write a restore plan for extent-restore.

Everything here works on a COPY of the metadata (see scripts/collect-metadata.sh). It never touches the damaged volume.

Directory layout (all paths are arguments):
  IMAGES/   what collect-metadata.sh produced: manifest.txt, chunk-*.img, dev-first-1MiB.img, chunks.txt, dev-size.txt, root-tree.txt
  WORK/     where this tool writes: scan/ (extracted tree blocks, pickles), plan.txt, plan-manifest.tsv, plan-skipped.tsv

Typical run:
  python3 btrfs_undelete.py --images IMAGES --work WORK scan   --tree 257
  python3 btrfs_undelete.py --images IMAGES --work WORK tree   --tree 257
  python3 btrfs_undelete.py --images IMAGES --work WORK chunks
  python3 btrfs_undelete.py --images IMAGES --work WORK plan   --config plan-config.json
  # ... run extent-restore on the NAS, then:
  python3 btrfs_undelete.py --images IMAGES --work WORK verify --local-root /path/to/restored/tree
"""
import argparse, bisect, collections, json, os, pickle, re, shutil, struct, subprocess, sys, time

REC = 16 + 16384                 # extracted record: u64 logical, u64 generation, 16 KiB tree block
NODESIZE = 16384
ROOT_ITEM, INODE_ITEM, INODE_REF, INODE_EXTREF, XATTR_ITEM, DIR_ITEM, DIR_INDEX, EXTENT_DATA = 132, 1, 12, 13, 24, 84, 96, 108
EXTENT_ITEM, DEV_EXTENT, CHUNK_ITEM = 168, 204, 228
S_IFMT, S_IFDIR, S_IFREG, S_IFLNK = 0o170000, 0o040000, 0o100000, 0o120000
TYPE_NAMES = {1: "INODE_ITEM", 12: "INODE_REF", 13: "INODE_EXTREF", 24: "XATTR_ITEM", 84: "DIR_ITEM", 96: "DIR_INDEX", 108: "EXTENT_DATA"}


# --------------------------------------------------------------------------------------------------------- helpers
def read_blocks(path):
    """yield (logical, generation, 16 KiB block) from a file written by scanmeta"""
    with open(path, "rb") as f:
        while True:
            rec = f.read(REC)
            if len(rec) < REC:
                break
            logical, gen = struct.unpack_from("<QQ", rec, 0)
            yield logical, gen, rec[16:]


def leaf_items(b):
    """yield (objectid, type, offset, data) for every item in a leaf block"""
    nr = struct.unpack_from("<I", b, 96)[0]
    for i in range(nr):
        oid, typ, off, doff, dsz = struct.unpack_from("<QBQII", b, 101 + i * 25)
        s = 101 + doff
        if s + dsz > len(b) or s < 101 + nr * 25:        # corrupt item pointer
            continue
        yield oid, typ, off, b[s:s + dsz]


def hx(s):
    return s.encode("utf-8", "surrogateescape").hex()


def paths(args):
    scan = os.path.join(args.work, "scan")
    os.makedirs(scan, exist_ok=True)
    return args.images, args.work, scan


def tree_ctransid(images, tree):
    """last-change generation of the deleted subvolume's root item, from the root-tree dump (or None)"""
    p = os.path.join(images, "root-tree.txt")
    if not os.path.exists(p):
        return None
    pat = re.compile(r"key \(%d ROOT_ITEM 0\)" % tree)
    seen = False
    for line in open(p, errors="replace"):
        if pat.search(line):
            seen = True
        elif seen:
            m = re.search(r"ctransid (\d+)", line)
            if m:
                return int(m.group(1))
            if "key (" in line:
                return None
    return None


# --------------------------------------------------------------------------------------------------------- scan
def cmd_scan(args):
    images, work, scan = paths(args)
    exe = args.scanmeta or shutil.which("scanmeta") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scanmeta")
    if not os.path.exists(exe):
        sys.exit("scanmeta not found. Build it:  gcc -O2 -msse4.2 -o scanmeta src/scanmeta.c   and pass --scanmeta PATH")
    # trees to extract: the deleted subvolume, the extent tree (2), the chunk tree (3), the device tree (4)
    cmd = [exe, images, scan, str(args.tree), "2", "3", "4"]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    ct = tree_ctransid(images, args.tree)
    print(f"\nroot item of tree {args.tree}: last-change generation (ctransid) = {ct}")
    if ct:
        print(f"suggested --before-gen for the next step: {ct - 500}")
    leaves = collections.Counter()
    for logical, gen, b in read_blocks(os.path.join(scan, f"owner-{args.tree}.bin")):
        if b[100] == 0:
            leaves[gen // 100000 * 100000] += 1
    print("surviving leaves of the deleted tree by generation bucket (100k wide):", dict(sorted(leaves.items())[-8:]), "...")


# --------------------------------------------------------------------------------------------------------- tree
def cmd_tree(args):
    images, work, scan = paths(args)
    T = args.before_gen
    if T is None:
        ct = tree_ctransid(images, args.tree)
        if ct is None:
            sys.exit("cannot work out --before-gen (no root-tree.txt entry): pass --before-gen N (a generation just before the deletion started)")
        T = ct - 500
        print(f"--before-gen not given: using ctransid-500 = {T}")
    WANT = {INODE_ITEM, INODE_REF, INODE_EXTREF, DIR_INDEX, DIR_ITEM, EXTENT_DATA}
    t0 = time.time()
    leaves = []                                  # [gen, first_key, last_key, {key: data}]
    for logical, gen, b in read_blocks(os.path.join(scan, f"owner-{args.tree}.bin")):
        if b[100] != 0:
            continue
        nr = struct.unpack_from("<I", b, 96)[0]
        if nr == 0:
            continue
        allkeys = [struct.unpack_from("<QBQ", b, 101 + i * 25) for i in range(nr)]
        d = {}
        for oid, typ, off, data in leaf_items(b):
            if typ in WANT:
                d[(oid, typ, off)] = data
        leaves.append([gen, allkeys[0], allkeys[-1], d])
    leaves.sort(key=lambda l: l[1])
    print(f"leaves: {len(leaves):,}  ({time.time()-t0:.0f}s)")
    # AUTHORITATIVE VERSION of every item: the one in the newest surviving leaf (generation <= T) whose key range covers the key.
    # If that leaf lacks the key, the item had already been deleted before the mass delete -> a "ghost", dropped.
    allk = sorted({k for l in leaves for k in l[3]})
    live = {}
    dropped = collections.Counter()
    active = []
    p = 0
    for K in allk:
        while p < len(leaves) and leaves[p][1] <= K:
            active.append(leaves[p])
            p += 1
        active = [l for l in active if l[2] >= K]
        cands = [l for l in active if l[0] <= T]
        if cands:
            auth = max(cands, key=lambda l: l[0])
            if K in auth[3]:
                live[K] = auth[3][K]
            else:
                dropped[K[1]] += 1
        else:                                    # only leaves from the deletion window hold it: keep (state during the mass delete)
            post = [l for l in active if K in l[3]]
            if post:
                live[K] = max(post, key=lambda l: l[0])[3][K]
    print(f"unique item keys: {len(allk):,}  live: {len(live):,}  dropped as already-deleted: {sum(dropped.values()):,} "
          f"{ {TYPE_NAMES.get(k, k): v for k, v in dropped.items()} }  ({time.time()-t0:.0f}s)")

    def nm(raw):
        return raw.decode("utf-8", "surrogateescape")

    inodes, iref, dchild, extents, inline = {}, collections.defaultdict(list), collections.defaultdict(list), collections.defaultdict(list), {}
    for (oid, typ, off), d in live.items():
        if typ == INODE_ITEM and len(d) >= 160:
            (g, tr, size, nbytes, bg, nlink, uid, gid, mode) = struct.unpack_from("<QQQQQIIII", d, 0)
            inodes[oid] = dict(ino=oid, size=size, nlink=nlink, uid=uid, gid=gid, mode=mode, mtime=struct.unpack_from("<Q", d, 136)[0], igen=g)
        elif typ == INODE_REF:
            pos = 0
            while pos + 10 <= len(d):
                idx, nl = struct.unpack_from("<QH", d, pos)
                pos += 10
                iref[oid].append((off, nm(d[pos:pos + nl]), idx))
                pos += nl
        elif typ == INODE_EXTREF:
            pos = 0
            while pos + 18 <= len(d):
                par, idx, nl = struct.unpack_from("<QQH", d, pos)
                pos += 18
                iref[oid].append((par, nm(d[pos:pos + nl]), idx))
                pos += nl
        elif typ in (DIR_ITEM, DIR_INDEX):
            pos = 0
            while pos + 30 <= len(d):
                child, ctyp, coff, tr, dlen, nlen, ftype = struct.unpack_from("<QBQQHHB", d, pos)
                pos += 30
                n_ = nm(d[pos:pos + nlen])
                pos += nlen + dlen
                if ctyp == INODE_ITEM:
                    dchild[child].append((oid, n_, ftype))
        elif typ == EXTENT_DATA and len(d) >= 21:
            g, ram, comp, enc, oenc, etype = struct.unpack_from("<QQBBHB", d, 0)
            if etype == 0:
                extents[oid].append((off, "inline", 0, 0, 0, ram, ram, comp))
                inline[(oid, off)] = bytes(d[21:])
            elif len(d) >= 53:
                dbn, dnb, eoff, nb = struct.unpack_from("<QQQQ", d, 21)
                extents[oid].append((off, "prealloc" if etype == 2 else "reg", dbn, dnb, eoff, nb, ram, comp))
    print(f"live inodes: {len(inodes):,}; with live INODE_REF: {len(iref):,}; with live dir entry: {len(dchild):,}")

    # parent/name per inode: prefer a live INODE_REF that a live directory entry of that parent confirms
    parent, name, how = {}, {}, collections.Counter()
    for ino in inodes:
        if ino == 256:
            continue
        refs, des = iref.get(ino, []), dchild.get(ino, [])
        chosen = None
        if refs:
            deset = {(pp, n_) for pp, n_, ft in des}
            conf = [r for r in refs if (r[0], r[1]) in deset]
            chosen = (conf or refs)[0][:2]
            how["inode_ref" + ("+confirmed" if conf else "")] += 1
        elif des:
            chosen = (des[0][0], des[0][1])
            how["dir_entry"] += 1
        if chosen:
            parent[ino], name[ino] = chosen
        else:
            how["no-name"] += 1
    print("parent source:", dict(how))
    groups = collections.defaultdict(list)       # two siblings with the same name: the newest inode wins
    for ino, par in parent.items():
        groups[(par, name[ino])].append(ino)
    dropped_ino, conflicts = set(), 0
    for key, inos in groups.items():
        if len(inos) > 1:
            conflicts += 1
            keep = max(inos)
            dropped_ino.update(i for i in inos if i != keep)
    print(f"sibling name conflicts: {conflicts}  -> inodes dropped: {len(dropped_ino)}")
    memo = {}

    def chain_state(ino, depth=0):
        if ino in memo:
            return memo[ino]
        if ino in dropped_ino:
            memo[ino] = (None, False)
            return memo[ino]
        if ino not in parent:
            memo[ino] = (f"_lost+found/ino{ino}", False)
            return memo[ino]
        par, n_ = parent[ino], name[ino]
        if depth > 400:
            memo[ino] = (f"_lost+found/loop{ino}", False)
            return memo[ino]
        if par == 256:
            memo[ino] = (n_, True)
            return memo[ino]
        if par not in inodes:
            memo[ino] = (f"_lost+found/missing-parent-ino{par}/{n_}", False)
            return memo[ino]
        pp, att = chain_state(par, depth + 1)
        memo[ino] = (None, False) if pp is None else (pp + "/" + n_, att)
        return memo[ino]

    tree = []
    for ino, it in inodes.items():
        if ino == 256:
            continue
        path, att = chain_state(ino)
        if path is None:
            continue
        kind = {S_IFDIR: "dir", S_IFREG: "file", S_IFLNK: "symlink"}.get(it["mode"] & S_IFMT, "other")
        ex = sorted(extents.get(ino, []))
        cover, status = 0, "n/a"
        if kind == "file":
            iv = sorted((e[0], e[0] + e[6 if e[1] == "inline" else 5]) for e in ex)
            pos = 0
            for s, e_ in iv:
                if e_ <= pos:
                    continue
                cover += e_ - max(s, pos)
                pos = e_
            status = "complete" if (it["size"] == 0 or cover >= it["size"]) else ("partial" if cover else "no-extents")
        tree.append(dict(it, kind=kind, path=path, attached=att, status=status, covered=cover, extents=ex))
    byp = collections.defaultdict(list)
    for r in tree:
        byp[r["path"]].append(r)
    col = {p_: v for p_, v in byp.items() if len(v) > 1}
    print(f"\nREBUILT TREE: {len(tree):,} entries; path collisions: {len(col)}")
    cnt = collections.Counter((r["kind"], r["status"], r["attached"]) for r in tree)
    for k in sorted(cnt):
        print(f"   {k[0]:8} {k[1]:10} attached={str(k[2]):5} {cnt[k]:>8,}")
    print(f"regular-file bytes: {sum(r['size'] for r in tree if r['kind']=='file')/2**40:.3f} TiB")
    pickle.dump((tree, inline), open(os.path.join(scan, "tree.pkl"), "wb"))
    agg = collections.defaultdict(lambda: [0, 0])
    for r in tree:
        if r["kind"] == "file":
            t = r["path"].split("/")[0]
            agg[t][0] += 1
            agg[t][1] += r["size"]
    print("\ntop-level folders (files, GiB):")
    for t, (n, sz) in sorted(agg.items(), key=lambda x: -x[1][1])[:40]:
        print(f"   {n:>8,} files {sz/2**30:>10,.1f} GiB   {t}")
    print(f"done ({time.time()-t0:.0f}s) -> {os.path.join(scan, 'tree.pkl')}")


# --------------------------------------------------------------------------------------------------------- chunks
def cmd_chunks(args):
    """logical -> physical for DATA chunks: the current chunk list plus chunks BTRFS deleted when they emptied, recovered from
    stale chunk-tree and device-tree leaves"""
    images, work, scan = paths(args)
    cur_phys, cur_logical = [], {}
    for l in open(os.path.join(images, "chunks.txt")):
        lg, ln, typ, idx, devid, off = l.split()
        lg, ln, off = int(lg), int(ln), int(off)
        cur_phys.append((off, off + ln, lg, typ))
        cur_logical[lg] = (ln, typ)
    cur_phys.sort()
    cur_starts = [c[0] for c in cur_phys]

    def overlaps_current(s, e):
        i = bisect.bisect_right(cur_starts, s) - 1
        for j in range(max(i, 0), len(cur_phys)):
            if cur_phys[j][0] >= e:
                break
            if cur_phys[j][1] > s:
                return cur_phys[j]
        return None

    cand = collections.defaultdict(set)          # logical chunk start -> {(physical, length, source, gen)}
    for logical, gen, b in read_blocks(os.path.join(scan, "owner-3.bin")):
        if b[100] != 0:
            continue
        for oid, typ, off, d in leaf_items(b):
            if typ == CHUNK_ITEM and len(d) >= 80:
                length, owner, slen, ctype = struct.unpack_from("<QQQQ", d, 0)
                ns, = struct.unpack_from("<H", d, 44)
                if ctype & 1 and ns >= 1:
                    devid, poff = struct.unpack_from("<QQ", d, 48)
                    cand[off].add((poff, length, "chunktree", gen))
    for logical, gen, b in read_blocks(os.path.join(scan, "owner-4.bin")):
        if b[100] != 0:
            continue
        for oid, typ, off, d in leaf_items(b):
            if typ == DEV_EXTENT and len(d) >= 32:
                ctree, cobj, coff, length = struct.unpack_from("<QQQQ", d, 0)
                cand[coff].add((off, length, "devtree", gen))
    print(f"logical chunk starts mentioned by surviving chunk-tree/dev-tree blocks: {len(cand)}")
    final, conflicts = {}, 0
    for lg, opts in cand.items():
        if lg in cur_logical:
            continue
        phys = sorted(set((p, l) for p, l, s, g in opts))
        if len(phys) > 1:
            conflicts += 1
        best_gen = max(o[3] for o in opts)
        top = sorted({(o[0], o[1]) for o in opts if o[3] == best_gen})
        p, l = top[0][0], top[0][1]
        if len(top) > 1:                         # two different locations, equally recent: do not guess
            final[lg] = (l, p, "AMBIGUOUS")
            continue
        final[lg] = (l, p, "usable" if overlaps_current(p, p + l) is None else "REUSED-by-current-chunk")
    print(f"removed-chunk candidates: {len(final)}; with >1 distinct physical option: {conflicts}; status: {dict(collections.Counter(v[2] for v in final.values()))}")
    cmap = {}
    for l in open(os.path.join(images, "chunks.txt")):
        lg, ln, typ, idx, devid, off = l.split()
        if typ == "DATA" and idx == "0":
            cmap[int(lg)] = (int(ln), int(off), "current")
    for lg, (l, p, st) in final.items():
        if st == "usable":
            cmap[lg] = (l, p, "removed")
    pickle.dump(cmap, open(os.path.join(scan, "cmap.pkl"), "wb"))
    tp = os.path.join(scan, "tree.pkl")
    if os.path.exists(tp):
        tree, inline = pickle.load(open(tp, "rb"))
        keys = sorted(cmap)
        ext = {}
        for r in tree:
            if r["kind"] == "file":
                for (foff, kind, dbn, dnb, eoff, nb, ram, comp) in r["extents"]:
                    if kind == "reg" and dbn:
                        ext[(dbn, dnb)] = 1
        stat, statb = collections.Counter(), collections.Counter()
        for (dbn, dnb) in ext:
            i = bisect.bisect_right(keys, dbn) - 1
            if i < 0 or dbn >= keys[i] + cmap[keys[i]][0]:
                s = "no-chunk-known"
            elif dbn + dnb > keys[i] + cmap[keys[i]][0]:
                s = "crosses-chunk-boundary"
            else:
                s = "mapped-" + cmap[keys[i]][2]
            stat[s] += 1
            statb[s] += dnb
        print("\nrecovered data extents with the combined map:")
        for k in stat:
            print(f"  {k:26} {stat[k]:>9,} extents  {statb[k]/2**40:8.3f} TiB")
    print(f"-> {os.path.join(scan, 'cmap.pkl')}")


# --------------------------------------------------------------------------------------------------------- plan
def cmd_plan(args):
    images, work, scan = paths(args)
    cfg = {"skip_top": [], "tiers": [], "default_tier": "1-first", "default_uid": 0, "default_gid": 0}
    if args.config:
        cfg.update(json.load(open(args.config)))
    skip_top = set(cfg["skip_top"]) | set(args.skip_top or [])
    tiers = [(t["name"], set(t["tops"])) for t in cfg["tiers"]]
    default_tier = cfg["default_tier"]
    del_gen = args.del_gen
    if del_gen is None:
        del_gen = tree_ctransid(images, args.tree) if args.tree else None
    if del_gen is None:
        sys.exit("need --del-gen N (the deleted subtree's last-change generation, i.e. ctransid in root-tree.txt) or --tree ID to read it")
    devsize = int(open(os.path.join(images, "dev-size.txt")).read().split()[0])
    tree, inline = pickle.load(open(os.path.join(scan, "tree.pkl"), "rb"))
    cmap = pickle.load(open(os.path.join(scan, "cmap.pkl"), "rb"))
    ckeys = sorted(cmap)

    def phys_of(lb, ln):
        i = bisect.bisect_right(ckeys, lb) - 1
        if i < 0:
            return None
        base = ckeys[i]
        clen, cphys, st = cmap[base]
        if lb >= base + clen or lb + ln > base + clen:
            return None
        return cphys + (lb - base)

    newalloc = {}                                # data extents allocated AFTER the deletion (from every surviving extent-tree leaf)
    for logical, gen, b in read_blocks(os.path.join(scan, "owner-2.bin")):
        if b[100] != 0:
            continue
        nr = struct.unpack_from("<I", b, 96)[0]
        for i in range(nr):
            oid, typ, off, doff, dsz = struct.unpack_from("<QBQII", b, 101 + i * 25)
            if typ == EXTENT_ITEM and dsz >= 24:
                refs, gen_, flags = struct.unpack_from("<QQQ", b, 101 + doff)
                if flags & 1 and gen_ > del_gen:
                    newalloc[(oid, off)] = gen_
    na = sorted(newalloc)
    na_starts = [x[0] for x in na]

    def overlaps_new(s, e):
        i = bisect.bisect_right(na_starts, s) - 1
        for k in range(max(i - 1, 0), min(i + 3, len(na))):
            if na[k][0] < e and na[k][0] + na[k][1] > s:
                return True
        return False

    print(f"extents allocated after the deletion (generation > {del_gen}): {len(na):,}, {sum(x[1] for x in na)/2**30:.2f} GiB - none may overlap what we restore")

    def top(p):
        return p.split("/")[0]

    tier_of_top = {}
    for name_, tops in tiers:
        for t in tops:
            tier_of_top[t] = name_
    tier_order = [default_tier] + [n for n, _ in tiers]

    def tier_for(p):
        return tier_of_top.get(top(p), default_tier)

    dirs = {r["path"]: r for r in tree if r["kind"] == "dir" and top(r["path"]) not in skip_top and r["attached"]}
    files_sel, skipped = [], []
    for r in tree:
        if r["kind"] != "file" or not r["attached"] or top(r["path"]) in skip_top:
            continue
        why = None
        if r["status"] != "complete":
            why = f"extent map {r['status']}"
        else:
            for (foff, kind, dbn, dnb, eoff, nb, ram, comp) in r["extents"]:
                if kind == "reg":
                    if comp:
                        why = "compressed extent (not supported)"
                        break
                    if dbn == 0:
                        continue
                    if phys_of(dbn, dnb) is None:
                        why = "extent not mapped to disk"
                        break
                    if overlaps_new(dbn, dbn + dnb):
                        why = "extent overwritten after deletion"
                        break
        if why:
            skipped.append((r["path"], r["size"], why))
            continue
        files_sel.append(r)
    print(f"files selected: {len(files_sel):,}   skipped: {len(skipped)}")
    files_sel.sort(key=lambda r: (tier_order.index(tier_for(r["path"])), r["path"]))
    for i, r in enumerate(files_sel):
        r["pid"] = i
    need_dirs = set()
    for r in files_sel:
        parts = r["path"].split("/")
        for k in range(1, len(parts)):
            need_dirs.add("/".join(parts[:k]))
    need_dirs.update(dirs)
    lines = ["PLAN1"]
    for p in sorted(need_dirs, key=lambda x: (x.count("/"), x)):
        d = dirs.get(p)
        mode, uid, gid, mt = ((d["mode"] & 0o7777, d["uid"], d["gid"], d["mtime"]) if d else (0o755, cfg["default_uid"], cfg["default_gid"], 1700000000))
        lines.append("\t".join(["D", format(mode, "o"), str(uid), str(gid), str(mt), hx(p)]))
    for r in files_sel:
        lines.append("\t".join(["F", str(r["pid"]), str(r["size"]), format(r["mode"] & 0o7777, "o"), str(r["uid"]), str(r["gid"]), str(r["mtime"]), "0", hx(r["path"])]))
    tier_bytes, tier_files = collections.Counter(), collections.Counter()
    nE = nI = total_dev_bytes = 0
    for tname in tier_order:
        inl_lines, E = [], []
        for r in files_sel:
            if tier_for(r["path"]) != tname:
                continue
            tier_files[tname] += 1
            tier_bytes[tname] += r["size"]
            for (foff, kind, dbn, dnb, eoff, nb, ram, comp) in r["extents"]:
                if foff >= r["size"]:
                    continue
                if kind == "inline":
                    data = inline.get((r["ino"], foff), b"")[:max(0, r["size"] - foff)]
                    if data:
                        inl_lines.append("\t".join(["I", str(r["pid"]), str(foff), data.hex()]))
                elif kind == "reg" and dbn:
                    ln = min(nb, r["size"] - foff)
                    if ln <= 0:
                        continue
                    E.append((phys_of(dbn, dnb) + eoff, r["pid"], foff, ln))
        E.sort()
        merged = []
        for ph, pid, foff, ln in E:
            if merged and merged[-1][1] == pid and merged[-1][0] + merged[-1][3] == ph and merged[-1][2] + merged[-1][3] == foff:
                merged[-1] = (merged[-1][0], pid, merged[-1][2], merged[-1][3] + ln)
            else:
                merged.append((ph, pid, foff, ln))
        lines.extend(inl_lines)
        nI += len(inl_lines)
        for ph, pid, foff, ln in merged:
            assert ph + ln <= devsize, "extent beyond the end of the device"
            lines.append("\t".join(["E", str(pid), str(foff), str(ph), str(ln)]))
            nE += 1
            total_dev_bytes += ln
    plan_path = os.path.join(work, "plan.txt")
    open(plan_path, "w").write("\n".join(lines) + "\n")
    with open(os.path.join(work, "plan-manifest.tsv"), "w") as f:
        for r in files_sel:
            f.write(f"{r['pid']}\t{tier_for(r['path'])}\t{r['size']}\t{r['path']}\n")
    with open(os.path.join(work, "plan-skipped.tsv"), "w") as f:
        for p, s, w in skipped:
            f.write(f"{s}\t{w}\t{p}\n")
    print(f"dirs: {len(need_dirs):,}  files: {len(files_sel):,}  E records: {nE:,}  inline records: {nI:,}  plan size: {os.path.getsize(plan_path)/1e6:.1f} MB")
    print(f"bytes to copy from the device: {total_dev_bytes/2**40:.3f} TiB  (sum of file sizes {sum(r['size'] for r in files_sel)/2**40:.3f} TiB)")
    for t in tier_order:
        print(f"   tier {t:28} {tier_files[t]:>7,} files  {tier_bytes[t]/2**30:>9,.1f} GiB")
    if skipped:
        print("skipped examples:")
        for p, s, w in skipped[:8]:
            print("    ", w, "|", p[:90], f"({s} B)")
    print(f"-> {plan_path}")


# --------------------------------------------------------------------------------------------------------- verify
def cmd_verify(args):
    """after the restore: every planned file present with its exact size, nothing left as .restore-partial/.restore-FAILED"""
    images, work, scan = paths(args)
    have = {}
    if args.local_root:
        for r, d, fs in os.walk(args.local_root):
            for f in fs:
                fp = os.path.join(r, f)
                have[os.path.relpath(fp, args.local_root)] = os.path.getsize(fp)
    else:
        out = subprocess.run(args.ssh_cmd + [f"find {args.remote_root} -type f -printf '%s\\t%P\\n'"], capture_output=True, text=True).stdout
        for l in out.split("\n"):
            if "\t" in l:
                s, p = l.split("\t", 1)
                have[p] = int(s)
    exp, tier = {}, {}
    for l in open(os.path.join(work, "plan-manifest.tsv")):
        pid, t, size, path = l.rstrip("\n").split("\t", 3)
        exp[path] = int(size)
        tier[path] = t
    missing = [p for p in exp if p not in have]
    wrong = [p for p in exp if p in have and have[p] != exp[p]]
    extra = [p for p in have if p not in exp]
    partial = [p for p in extra if p.endswith(".restore-partial") or p.endswith(".restore-FAILED")]
    print(f"planned files: {len(exp):,}   present: {len(have):,}")
    print(f"  missing: {len(missing)}   wrong size: {len(wrong)}   leftover .restore-partial/FAILED: {len(partial)}   other extra files: {len(extra) - len(partial)}")
    by = collections.Counter(tier[p] for p in exp if p in have and have[p] == exp[p])
    print("  fully matching, by tier:", dict(sorted(by.items())))
    for name_, lst in (("missing", missing), ("wrong size", wrong), ("partial", partial)):
        for p in lst[:8]:
            print(f"   {name_}: {p}")
    ok = not missing and not wrong and not partial
    print("RESULT:", "ALL FILES PRESENT WITH EXACT SIZES" if ok else "PROBLEMS FOUND")
    sys.exit(0 if ok else 1)


# --------------------------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Rebuild the files of a deleted BTRFS subvolume from leftover metadata.")
    ap.add_argument("--images", required=True, help="directory produced by collect-metadata.sh")
    ap.add_argument("--work", required=True, help="output directory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="find intact tree blocks in the metadata copy (needs the scanmeta binary)")
    s.add_argument("--tree", type=int, required=True, help="subvolume id of the deleted share (see: btrfs subvolume list -d)")
    s.add_argument("--scanmeta")
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("tree", help="rebuild the file tree as it was before the deletion")
    s.add_argument("--tree", type=int, required=True)
    s.add_argument("--before-gen", type=int, help="leaves with a generation >= this belong to the deletion itself (default: ctransid-500)")
    s.set_defaults(fn=cmd_tree)
    s = sub.add_parser("chunks", help="rebuild the logical->physical map, including chunks BTRFS deleted when they emptied")
    s.set_defaults(fn=cmd_chunks)
    s = sub.add_parser("plan", help="check for reused space and write the restore plan")
    s.add_argument("--tree", type=int, help="subvolume id (used to read ctransid for --del-gen)")
    s.add_argument("--del-gen", type=int, help="data allocated after this generation counts as 'reused' (default: ctransid)")
    s.add_argument("--config", help="JSON: {skip_top:[...], tiers:[{name, tops:[...]}], default_tier}")
    s.add_argument("--skip-top", action="append", help="top-level folder to leave out (repeatable)")
    s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("verify", help="compare a restored tree with the plan")
    s.add_argument("--local-root", help="restored tree on this machine")
    s.add_argument("--remote-root", help="restored tree on the NAS (with --ssh-cmd)")
    s.add_argument("--ssh-cmd", nargs="+", default=["ssh"], help="ssh command prefix incl. host, e.g. ssh user@nas")
    s.set_defaults(fn=cmd_verify)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
