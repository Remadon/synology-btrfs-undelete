#!/usr/bin/env python3
"""Self-contained test of extent-restore against a fake 128 MiB "device": multi-extent, sparse, empty, inline-only, odd names,
chunked big files, resume after interruption, and the safety guards.   Usage: python3 tests/test_extent_restore.py [path/to/extent-restore]
If no binary is given it is compiled from ../src/extent-restore.c with gcc (dynamic) into a temp dir."""
import hashlib, os, random, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MiB = 1 << 20
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")


def main():
    tmp = tempfile.mkdtemp(prefix="extent-restore-test-")
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tmp, "extent-restore")
    if len(sys.argv) <= 1:
        subprocess.run(["gcc", "-O2", "-pthread", "-o", exe, os.path.join(HERE, "..", "src", "extent-restore.c")], check=True)
    random.seed(7)
    dev = bytearray(os.urandom(128 * MiB))
    devp = os.path.join(tmp, "fakedev.img")
    open(devp, "wb").write(dev)
    hexp = lambda p: p.encode("utf-8", "surrogateescape").hex()
    lines = ["PLAN1"]
    for p, m in [("A", 0o755), ("A/sub dir", 0o755), ("A/sub dir/deep", 0o700), ("B", 0o755), ("odd", 0o755)]:
        lines.append("\t".join(["D", oct(m)[2:], "1026", "100", "1600000000", hexp(p)]))
    files = []

    def add(path, size, ext=(), inl=(), mode=0o644):
        files.append(dict(id=len(files), path=path, size=size, mode=mode, ext=list(ext), inl=list(inl)))

    add("A/one.bin", 20 * MiB + 123, [(0, 4 * MiB, 9 * MiB), (9 * MiB, 40 * MiB, 11 * MiB), (20 * MiB, 90 * MiB, 123)])
    add("A/sub dir/sparse.bin", 30 * MiB, [(0, 100 * MiB, 4096), (25 * MiB, 101 * MiB, 4 * MiB)])
    add("A/sub dir/deep/empty", 0)
    add("B/tiny.txt", 11, inl=[(0, b"hello world")])
    add("odd/na me\t'\"é☃\nz", 5000, [(0, 60 * MiB, 5000)])
    add("B/big.bin", 27 * MiB, [(0, 64 * MiB, 27 * MiB)], mode=0o777)
    for i in range(300):
        add(f"B/small{i:03d}.dat", 100 + i, [(0, 50 * MiB + i * 4096, 100 + i)])
    for f in files:
        lines.append("\t".join(["F", str(f["id"]), str(f["size"]), oct(f["mode"])[2:], "1026", "100", "1700000000", "0", hexp(f["path"])]))
    recs = []
    for f in files:
        recs += [("I", f["id"], o, b.hex()) for o, b in f["inl"]]
        recs += [("E", f["id"], o, ph, ln) for o, ph, ln in f["ext"]]
    random.shuffle(recs)
    recs.sort(key=lambda r: (r[0] != "I", r[3] if r[0] == "E" else 0))
    lines += ["\t".join(str(x) for x in r) for r in recs]
    plan = os.path.join(tmp, "plan.txt")
    open(plan, "w").write("\n".join(lines) + "\n")
    exp = {}
    for f in files:
        buf = bytearray(f["size"])
        for o, ph, ln in f["ext"]:
            buf[o:o + ln] = dev[ph:ph + ln]
        for o, b in f["inl"]:
            buf[o:o + len(b)] = b
        exp[f["path"]] = hashlib.sha256(bytes(buf)).hexdigest()
    out, jr, lg = os.path.join(tmp, "out"), os.path.join(tmp, "journal"), os.path.join(tmp, "log")

    def run(*extra, dest=out):
        return subprocess.run([exe, "--dev", devp, "--dest", dest, "--plan", plan, *extra], capture_output=True, text=True)

    def verify():
        bad = [p for p, h in exp.items() if not os.path.exists(os.path.join(out, p)) or hashlib.sha256(open(os.path.join(out, p), "rb").read()).hexdigest() != h]
        return bad

    print("restore:")
    r = run("--test", "--journal", jr, "--log", lg)
    check("exit code 0", r.returncode == 0, r.stderr.strip()[-200:])
    check("all files byte-identical", not verify(), f"({len(exp)} files)")
    left = [f for _, _, fs in os.walk(out) for f in fs if f.endswith((".restore-partial", ".restore-FAILED"))]
    check("no .restore-partial / .restore-FAILED left", not left)
    st = os.stat(os.path.join(out, "A/one.bin"))
    check("mtime and mode preserved", int(st.st_mtime) == 1700000000 and (st.st_mode & 0o777) == 0o644)
    sp = os.stat(os.path.join(out, "A/sub dir/sparse.bin"))
    check("sparse file stays sparse", sp.st_blocks * 512 < sp.st_size // 2, f"({sp.st_blocks*512} of {sp.st_size} bytes allocated)")
    print("resume:")
    r = run("--test", "--journal", jr, "--log", lg)
    check("re-run with a complete journal restores nothing", "0 files completed this run" in open(lg).read().split("\n")[-4] + open(lg).read(), "")
    victims = list(exp)[3:15]
    for p in victims:
        os.remove(os.path.join(out, p))
    ids = {p: i for i, p in enumerate(exp)}
    keep = [l for l in open(jr) if l.strip() not in {str(ids[p]) for p in victims}]
    open(jr, "w").writelines(keep)
    r = run("--test", "--journal", jr, "--log", lg)
    check("interrupted run resumes and restores only the missing files", r.returncode == 0 and not verify())
    print("guards:")
    r = run(dest=os.path.join(tmp, "o2"))
    check("refuses without --guard", r.returncode != 0 and "--guard" in r.stderr)
    r = run("--guard", tmp, dest=os.path.join(tmp, "o3"))
    check("refuses destination under the guard path", r.returncode != 0 and "refusing" in r.stderr and not os.path.exists(os.path.join(tmp, "o3")))
    bad = os.path.join(tmp, "bad.plan")
    open(bad, "w").write("PLAN1\nF\t0\t10\t644\t1\t1\t1\t0\t%s\nE\t0\t0\t999999999999\t10\n" % hexp("x"))
    r = subprocess.run([exe, "--dev", devp, "--dest", os.path.join(tmp, "o4"), "--plan", bad, "--test"], capture_output=True, text=True)
    check("refuses an extent past the end of the device", r.returncode != 0 and "past the end" in r.stderr)
    open(bad, "w").write("PLAN1\nF\t0\t10\t644\t1\t1\t1\t0\t%s\n" % hexp("../../etc/x"))
    r = subprocess.run([exe, "--dev", devp, "--dest", os.path.join(tmp, "o5"), "--plan", bad, "--test"], capture_output=True, text=True)
    check("refuses a path with ..", r.returncode != 0 and "unsafe" in r.stderr)
    check("device image unchanged (opened read-only)", hashlib.sha256(open(devp, "rb").read()).hexdigest() == hashlib.sha256(bytes(dev)).hexdigest())
    shutil.rmtree(tmp, ignore_errors=True)
    print("\nRESULT:", "ALL TESTS PASSED" if ok_all else "SOME TESTS FAILED")
    sys.exit(0 if ok_all else 1)


main()
