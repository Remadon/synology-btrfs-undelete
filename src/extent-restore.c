// extent-restore.c - rebuild files from raw extents of a block device and write them into a destination directory.
//
// Part of synology-btrfs-undelete. Given a "plan" (which byte ranges of the device belong to which file, plus paths, owners,
// modes and times) it copies the ranges into ordinary files. It does NOT parse btrfs; the plan comes from make_plan (py/).
//
// Build:  gcc -O2 -static -pthread -o extent-restore extent-restore.c      (static, so it runs on a NAS with an old libc)
//
// SAFETY: the source device is opened O_RDONLY and is only ever read with pread(). Nothing in this program writes to it.
//         The destination, journal and log must not be under --guard (the mount point of the damaged volume) nor on the same
//         filesystem as it, otherwise the program refuses to start.
//
// usage: extent-restore --dev /dev/mapper/cachedev_0 --dest /volume1/Recovered/Restored --plan plan.txt --guard /volume2
//                     [--journal FILE] [--log FILE] [--dry-run] [--test]
//
// plan format (tab separated, paths/data hex encoded):
//   PLAN1
//   D <mode-octal> <uid> <gid> <mtime> <pathhex>                      directory
//   F <id> <size> <mode-octal> <uid> <gid> <mtime> <nchunks> <pathhex>   file (nchunks filled in by the loader; ignored)
//   E <id> <file-offset> <device-offset> <length>                      copy bytes from the device into the file
//   I <id> <file-offset> <datahex>                                     inline data
// Files are written as <path>.restore-partial and renamed on completion; completed ids are appended to the journal so
// an interrupted run can be resumed by running the same command again.
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <pthread.h>
#include <time.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/ioctl.h>
#include <sys/vfs.h>
#include <sys/syscall.h>
#include <linux/fs.h>

#define CHUNK (8u << 20)          // 8 MiB per queued piece
#define QSLOTS 24                 // ~192 MiB of buffering
#define MAXOPEN 256

typedef struct { uint64_t size; uint32_t mode, uid, gid; uint64_t mtime; char *path; uint32_t remaining; uint8_t failed, done; int fd; } File;
typedef struct { uint8_t kind; uint32_t id; uint64_t foff, doff, len; uint8_t *inl; } Rec;   // kind 'E' or 'I'
typedef struct { uint32_t mode, uid, gid; uint64_t mtime; char *path; } Dir;
typedef struct { uint32_t id; uint64_t foff; size_t len; uint8_t *buf; int eof; } Slot;

static File *files; static uint32_t nfiles, capfiles;
static Rec *recs; static size_t nrecs, caprecs;
static Dir *dirs; static size_t ndirs, capdirs;
static const char *dest, *devpath, *planpath, *journalpath, *logpath, *guardpath;
static int dry_run, test_mode, devfd = -1, jfd = -1;
static uint64_t devsize;

static Slot q[QSLOTS]; static int qhead, qtail, qcount;
static pthread_mutex_t qm = PTHREAD_MUTEX_INITIALIZER; static pthread_cond_t qnotfull = PTHREAD_COND_INITIALIZER, qnotempty = PTHREAD_COND_INITIALIZER;

static uint64_t total_bytes, done_bytes, files_done, files_failed, io_errors, files_total_todo;
static int openlist[MAXOPEN]; static int nopen, evict_pos;
static FILE *logf;

static void die(const char *fmt, ...) __attribute__((noreturn));
#include <stdarg.h>
static void die(const char *fmt, ...) { va_list ap; va_start(ap, fmt); fprintf(stderr, "extent-restore: FATAL: "); vfprintf(stderr, fmt, ap); fprintf(stderr, "\n"); exit(2); }
static void logmsg(const char *fmt, ...) { va_list ap; va_start(ap, fmt); vfprintf(logf, fmt, ap); va_end(ap); fputc('\n', logf); fflush(logf); }

static int hexval(int c) { if (c >= '0' && c <= '9') return c - '0'; if (c >= 'a' && c <= 'f') return c - 'a' + 10; if (c >= 'A' && c <= 'F') return c - 'A' + 10; return -1; }
static uint8_t *unhex(const char *s, size_t *outlen) {
    size_t n = strlen(s); if (n % 2) die("odd-length hex string");
    uint8_t *b = malloc(n / 2 + 1); if (!b) die("oom");
    for (size_t i = 0; i < n / 2; i++) { int h = hexval(s[2 * i]), l = hexval(s[2 * i + 1]); if (h < 0 || l < 0) die("bad hex"); b[i] = (uint8_t)(h * 16 + l); }
    b[n / 2] = 0; *outlen = n / 2; return b;
}
static void check_relpath(const char *p) {
    if (!*p || p[0] == '/') die("unsafe path (absolute/empty): %s", p);
    const char *s = p;
    while (*s) {
        const char *e = strchr(s, '/'); size_t l = e ? (size_t)(e - s) : strlen(s);
        if ((l == 2 && s[0] == '.' && s[1] == '.') || l == 0) die("unsafe path component in: %s", p);
        if (!e) break;
        s = e + 1;
    }
}

static void load_plan(void) {
    FILE *f = fopen(planpath, "r"); if (!f) die("cannot open plan %s: %s", planpath, strerror(errno));
    char *line = NULL; size_t cap = 0; ssize_t n; int first = 1; size_t lineno = 0;
    while ((n = getline(&line, &cap, f)) > 0) {
        lineno++; while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r')) line[--n] = 0;
        if (!n) continue;
        if (first) { if (strcmp(line, "PLAN1")) die("not a PLAN1 file"); first = 0; continue; }
        char *tok[10]; int nt = 0; char *p = line;
        while (nt < 10) { tok[nt++] = p; char *t = strchr(p, '\t'); if (!t) break; *t = 0; p = t + 1; }
        if (tok[0][0] == 'D' && nt == 6) {
            if (ndirs == capdirs) { capdirs = capdirs ? capdirs * 2 : 4096; dirs = realloc(dirs, capdirs * sizeof(Dir)); }
            size_t pl; uint8_t *path = unhex(tok[5], &pl); check_relpath((char *)path);
            Dir *d = &dirs[ndirs++]; d->mode = (uint32_t)strtoul(tok[1], NULL, 8); d->uid = (uint32_t)strtoul(tok[2], NULL, 10); d->gid = (uint32_t)strtoul(tok[3], NULL, 10);
            d->mtime = strtoull(tok[4], NULL, 10); d->path = (char *)path;
        } else if (tok[0][0] == 'F' && nt == 9) {
            uint32_t id = (uint32_t)strtoul(tok[1], NULL, 10);
            if (id >= capfiles) { uint32_t nc = capfiles ? capfiles : 4096; while (nc <= id) nc *= 2; files = realloc(files, nc * sizeof(File)); memset(files + capfiles, 0, (nc - capfiles) * sizeof(File)); capfiles = nc; }
            if (id + 1 > nfiles) nfiles = id + 1;
            size_t pl; uint8_t *path = unhex(tok[8], &pl); check_relpath((char *)path);
            File *fl = &files[id]; fl->size = strtoull(tok[2], NULL, 10); fl->mode = (uint32_t)strtoul(tok[3], NULL, 8); fl->uid = (uint32_t)strtoul(tok[4], NULL, 10);
            fl->gid = (uint32_t)strtoul(tok[5], NULL, 10); fl->mtime = strtoull(tok[6], NULL, 10); fl->path = (char *)path; fl->fd = -1; fl->remaining = 0;
            files_total_todo++;
        } else if ((tok[0][0] == 'E' && nt == 5) || (tok[0][0] == 'I' && nt == 4)) {
            if (nrecs == caprecs) { caprecs = caprecs ? caprecs * 2 : 65536; recs = realloc(recs, caprecs * sizeof(Rec)); }
            Rec *r = &recs[nrecs++]; memset(r, 0, sizeof *r); r->kind = (uint8_t)tok[0][0]; r->id = (uint32_t)strtoul(tok[1], NULL, 10); r->foff = strtoull(tok[2], NULL, 10);
            if (r->id >= nfiles || !files[r->id].path) die("plan line %zu references unknown file id %u", lineno, r->id);
            if (r->kind == 'E') { r->doff = strtoull(tok[3], NULL, 10); r->len = strtoull(tok[4], NULL, 10); }
            else { size_t il; r->inl = unhex(tok[3], &il); r->len = il; }
        } else die("bad plan line %zu: %.60s", lineno, line);
    }
    free(line); fclose(f);
    for (size_t i = 0; i < nrecs; i++) {                 // pieces per file (chunking applied at read time)
        Rec *r = &recs[i];
        uint64_t pieces = r->kind == 'I' ? 1 : (r->len + CHUNK - 1) / CHUNK; if (!pieces) pieces = 1;
        files[r->id].remaining += (uint32_t)pieces;
    }
}

static void ensure_dirs(void) {
    char full[4096];
    for (size_t i = 0; i < ndirs; i++) {
        snprintf(full, sizeof full, "%s/%s", dest, dirs[i].path);
        if (mkdir(full, 0755) && errno != EEXIST) die("mkdir %s: %s", full, strerror(errno));
    }
}

static void journal_add(uint32_t id) {
    char b[24]; int l = snprintf(b, sizeof b, "%u\n", id);
    if (write(jfd, b, l) != l) logmsg("WARNING: journal write failed");
}
static void load_journal(void) {
    FILE *f = fopen(journalpath, "r"); if (!f) return;
    unsigned long id; size_t n = 0;
    while (fscanf(f, "%lu", &id) == 1) { if (id < nfiles && files[id].path) { files[id].done = 1; n++; } }
    fclose(f); logmsg("journal: %zu files already complete, skipping them", n);
}

static void register_open(uint32_t id) {
    if (nopen < MAXOPEN) { for (int k = 0; k < MAXOPEN; k++) if (openlist[k] < 0) { openlist[k] = (int)id; nopen++; return; } }
    int vid = openlist[evict_pos];                         // table full: close the next one in the ring, it reopens on demand
    if (vid >= 0 && files[vid].fd >= 0) { close(files[vid].fd); files[vid].fd = -1; }
    openlist[evict_pos] = (int)id; evict_pos = (evict_pos + 1) % MAXOPEN;
}
static void unregister_open(uint32_t id) { for (int k = 0; k < MAXOPEN; k++) if (openlist[k] == (int)id) { openlist[k] = -1; nopen--; return; } }
static int open_partial(File *f, uint32_t id) {
    if (f->fd >= 0) return f->fd;
    char full[4200]; snprintf(full, sizeof full, "%s/%s.restore-partial", dest, f->path);
    int fd = open(full, O_WRONLY | O_CREAT, 0600);
    if (fd < 0) { logmsg("ERROR open %s: %s", full, strerror(errno)); return -1; }
    f->fd = fd; register_open(id);
    return fd;
}

static void finalize_file(uint32_t id) {
    File *f = &files[id]; char part[4200], fin[4200];
    snprintf(part, sizeof part, "%s/%s.restore-partial", dest, f->path); snprintf(fin, sizeof fin, "%s/%s", dest, f->path);
    int fd = open_partial(f, id);
    if (fd >= 0) {
        if (ftruncate(fd, (off_t)f->size)) logmsg("WARNING ftruncate %s: %s", fin, strerror(errno));
        if (fchown(fd, f->uid, f->gid)) logmsg("WARNING chown %s: %s", fin, strerror(errno));
        if (fchmod(fd, (mode_t)((f->mode & 0777) | 0600))) logmsg("WARNING chmod %s: %s", fin, strerror(errno));
        struct timespec ts[2] = { { (time_t)f->mtime, 0 }, { (time_t)f->mtime, 0 } };
        if (futimens(fd, ts)) logmsg("WARNING utimens %s: %s", fin, strerror(errno));
        close(fd); f->fd = -1; unregister_open(id);
    }
    if (f->failed) {
        char bad[4300]; snprintf(bad, sizeof bad, "%s.restore-FAILED", fin);
        if (rename(part, bad)) logmsg("ERROR rename %s: %s", part, strerror(errno));
        files_failed++; logmsg("FAILED file (read errors): %s", f->path);
    } else {
        if (rename(part, fin)) { logmsg("ERROR rename %s -> %s: %s", part, fin, strerror(errno)); files_failed++; }
        else { journal_add(id); files_done++; }
    }
}

static void *writer(void *arg) {
    (void)arg;
    for (;;) {
        pthread_mutex_lock(&qm);
        while (qcount == 0) pthread_cond_wait(&qnotempty, &qm);
        Slot s = q[qhead]; qhead = (qhead + 1) % QSLOTS; qcount--; pthread_cond_signal(&qnotfull); pthread_mutex_unlock(&qm);
        if (s.eof) return NULL;
        File *f = &files[s.id];
        int fd = open_partial(f, s.id);
        if (fd < 0) f->failed = 1;
        else {
            size_t off = 0;
            while (off < s.len) {
                ssize_t w = pwrite(fd, s.buf + off, s.len - off, (off_t)(s.foff + off));
                if (w < 0) { if (errno == EINTR) continue; logmsg("ERROR write %s: %s", f->path, strerror(errno)); f->failed = 1; break; }
                off += (size_t)w;
            }
        }
        done_bytes += s.len; free(s.buf);
        if (--f->remaining == 0) finalize_file(s.id);
    }
}
static void enqueue(uint32_t id, uint64_t foff, uint8_t *buf, size_t len, int eof) {
    pthread_mutex_lock(&qm);
    while (qcount == QSLOTS) pthread_cond_wait(&qnotfull, &qm);
    Slot *s = &q[qtail]; s->id = id; s->foff = foff; s->buf = buf; s->len = len; s->eof = eof; qtail = (qtail + 1) % QSLOTS; qcount++;
    pthread_cond_signal(&qnotempty); pthread_mutex_unlock(&qm);
}

static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }

int main(int argc, char **argv) {
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--dev") && i + 1 < argc) devpath = argv[++i];
        else if (!strcmp(argv[i], "--dest") && i + 1 < argc) dest = argv[++i];
        else if (!strcmp(argv[i], "--plan") && i + 1 < argc) planpath = argv[++i];
        else if (!strcmp(argv[i], "--journal") && i + 1 < argc) journalpath = argv[++i];
        else if (!strcmp(argv[i], "--guard") && i + 1 < argc) guardpath = argv[++i];
        else if (!strcmp(argv[i], "--log") && i + 1 < argc) logpath = argv[++i];
        else if (!strcmp(argv[i], "--dry-run")) dry_run = 1;
        else if (!strcmp(argv[i], "--test")) test_mode = 1;
        else if (!strcmp(argv[i], "--version")) { puts("extent-restore 1.0"); return 0; }
        else die("unknown argument: %s", argv[i]);
    }
    if (!devpath || !dest || !planpath) die("need --dev, --dest and --plan  (and --guard <mountpoint of the damaged volume> unless --test/--dry-run)");
    if (!guardpath && !test_mode && !dry_run) die("--guard <mount point of the damaged volume, e.g. /volume2> is required: the destination must NOT be on it");
    logf = stderr;
    // SAFETY, before anything is created: destination, journal and log must not be on the guarded (source) filesystem
    if (!test_mode && !dry_run) {
        const char *chk[3] = { dest, journalpath, logpath };
        struct statfs fg_; int have_guard = (statfs(guardpath, &fg_) == 0);
        for (int c = 0; c < 3; c++) {
            if (!chk[c]) continue;
            char anc[4096], rp[4096]; snprintf(anc, sizeof anc, "%s", chk[c]);
            struct stat st_;
            while (stat(anc, &st_)) { char *sl = strrchr(anc, '/'); if (!sl || sl == anc) break; *sl = 0; }
            if (!realpath(anc, rp)) snprintf(rp, sizeof rp, "%s", anc);
            size_t gl = strlen(guardpath); while (gl > 1 && guardpath[gl - 1] == '/') gl--;
            if ((strncmp(chk[c], guardpath, gl) == 0 && (chk[c][gl] == '/' || chk[c][gl] == 0)) || (strncmp(rp, guardpath, gl) == 0 && (rp[gl] == '/' || rp[gl] == 0))) die("%s is under %s - refusing", chk[c], guardpath);
            struct statfs f_; if (statfs(anc, &f_)) die("statfs %s: %s", anc, strerror(errno));
            if (have_guard && f_.f_fsid.__val[0] == fg_.f_fsid.__val[0] && f_.f_fsid.__val[1] == fg_.f_fsid.__val[1]) die("%s is on the same filesystem as %s - refusing", chk[c], guardpath);
        }
    }
    struct stat sd;
    if (stat(dest, &sd) && !dry_run) { if (mkdir(dest, 0755)) die("cannot create dest %s: %s", dest, strerror(errno)); }
    devfd = open(devpath, O_RDONLY); if (devfd < 0) die("cannot open %s read-only: %s", devpath, strerror(errno));
    if (ioctl(devfd, BLKGETSIZE64, &devsize)) { off_t e = lseek(devfd, 0, SEEK_END); if (e < 0) die("cannot size device"); devsize = (uint64_t)e; }
    load_plan();
    char jp[4200]; if (!journalpath) { snprintf(jp, sizeof jp, "%s/.extent-restore-journal", dest); journalpath = jp; }
    if (!test_mode && !dry_run) { size_t gl = strlen(guardpath); while (gl > 1 && guardpath[gl - 1] == '/') gl--;
        if ((strncmp(journalpath, guardpath, gl) == 0 && (journalpath[gl] == '/' || journalpath[gl] == 0)) || (logpath && strncmp(logpath, guardpath, gl) == 0 && (logpath[gl] == '/' || logpath[gl] == 0))) die("journal/log under %s - refusing", guardpath); }
    char lp[4200]; if (!logpath) { snprintf(lp, sizeof lp, "%s/.extent-restore.log", dest); logpath = lp; }
    if (!dry_run) { FILE *lf = fopen(logpath, "a"); if (lf) logf = lf; }
    logmsg("extent-restore: dev=%s (%llu bytes) dest=%s plan=%s: %u files, %zu dirs, %zu extents/inlines", devpath, (unsigned long long)devsize, dest, planpath, nfiles, ndirs, nrecs);
    // validate extents against the device size before touching anything
    for (size_t i = 0; i < nrecs; i++) if (recs[i].kind == 'E' && recs[i].doff + recs[i].len > devsize) die("extent %zu runs past the end of the device", i);
    if (dry_run) { uint64_t b = 0; for (size_t i = 0; i < nrecs; i++) b += recs[i].len; logmsg("dry run OK: %llu bytes would be restored", (unsigned long long)b); return 0; }
    for (int k = 0; k < MAXOPEN; k++) openlist[k] = -1;
    syscall(SYS_ioprio_set, 1 /*PROCESS*/, 0, (2 << 13) | 7);        // best-effort class, lowest priority: be gentle with the NAS
    if (nice(10) < 0) { /* ignore */ }
    jfd = open(journalpath, O_WRONLY | O_CREAT | O_APPEND, 0644); if (jfd < 0) die("cannot open journal %s: %s", journalpath, strerror(errno));
    ensure_dirs();
    load_journal();
    // files with no data pieces (empty / fully sparse) are finished immediately
    for (uint32_t id = 0; id < nfiles; id++) if (files[id].path && !files[id].done && files[id].remaining == 0) { files[id].remaining = 1; finalize_file(id); }
    for (size_t i = 0; i < nrecs; i++) if (!files[recs[i].id].done) total_bytes += recs[i].len;
    logmsg("to restore now: %.2f GiB", total_bytes / 1073741824.0);
    pthread_t th; pthread_create(&th, NULL, writer, NULL);
    double t0 = now(), tlast = t0; uint64_t lastbytes = 0;
    for (size_t i = 0; i < nrecs; i++) {
        Rec *r = &recs[i]; File *f = &files[r->id];
        if (f->done) continue;
        if (r->kind == 'I') {
            uint8_t *b = malloc(r->len ? r->len : 1); memcpy(b, r->inl, r->len); enqueue(r->id, r->foff, b, r->len, 0); continue;
        }
        uint64_t pos = 0;
        do {
            size_t n = (size_t)((r->len - pos) > CHUNK ? CHUNK : (r->len - pos));
            uint8_t *b = malloc(n ? n : 1); if (!b) die("oom");
            size_t got = 0; int err = 0;
            while (got < n) {
                ssize_t rd = pread(devfd, b + got, n - got, (off_t)(r->doff + pos + got));
                if (rd < 0) { if (errno == EINTR) continue; err = errno; break; }
                if (rd == 0) { err = EIO; break; }
                got += (size_t)rd;
            }
            if (err) { io_errors++; logmsg("READ ERROR (%s) at device offset %llu for %s", strerror(err), (unsigned long long)(r->doff + pos), f->path); memset(b + got, 0, n - got); f->failed = 1; }
            posix_fadvise(devfd, (off_t)(r->doff + pos), (off_t)n, POSIX_FADV_DONTNEED);
            enqueue(r->id, r->foff + pos, b, n, 0);
            pos += n;
        } while (pos < r->len);
        double t = now();
        if (t - tlast >= 15) {
            uint64_t d = done_bytes; double rate = (d - lastbytes) / (t - tlast) / 1048576.0;
            logmsg("progress: %.1f%% of %.1f GiB  | %.0f MiB/s | files done %llu failed %llu | elapsed %.0f min", total_bytes ? 100.0 * d / total_bytes : 100.0, total_bytes / 1073741824.0, rate,
                   (unsigned long long)files_done, (unsigned long long)files_failed, (t - t0) / 60);
            tlast = t; lastbytes = d;
        }
    }
    enqueue(0, 0, NULL, 0, 1); pthread_join(th, NULL);
    // directory attributes last (their mtimes were disturbed while files were created); deepest first
    for (size_t i = ndirs; i-- > 0;) {
        char full[4200]; snprintf(full, sizeof full, "%s/%s", dest, dirs[i].path);
        if (chown(full, dirs[i].uid, dirs[i].gid)) { /* best effort */ }
        chmod(full, (mode_t)((dirs[i].mode & 0777) | 0700));
        struct timespec ts[2] = { { (time_t)dirs[i].mtime, 0 }, { (time_t)dirs[i].mtime, 0 } };
        utimensat(AT_FDCWD, full, ts, 0);
    }
    fsync(jfd);
    logmsg("FINISHED: %llu files completed this run, %llu failed, %llu read errors, %.1f GiB written, %.0f min", (unsigned long long)files_done, (unsigned long long)files_failed,
           (unsigned long long)io_errors, done_bytes / 1073741824.0, (now() - t0) / 60);
    logmsg("==== RESTORE COMPLETE ====");
    return files_failed || io_errors ? 1 : 0;
}
