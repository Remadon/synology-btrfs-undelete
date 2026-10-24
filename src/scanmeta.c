// scanmeta.c - find every intact btrfs tree block inside the metadata chunk images.
// Build:  gcc -O2 -msse4.2 -o scanmeta scanmeta.c      (drop -msse4.2 on CPUs without it: slower, same result)
// A block counts as intact only if its header fsid matches, its stored logical address equals the
// address it sits at, and its crc32c checksum matches. Read-only on the inputs.
//
// usage: scanmeta <imgdir> <outdir> [owner-to-extract ...]
//   reads  <imgdir>/manifest.txt  (lines: logical length type devoffset filename)
//          <imgdir>/dev-first-1MiB.img (superblock at 64 KiB gives the fsid)
//   writes <outdir>/blocks.tsv    logical gen owner level nritems manifest-line offset
//          <outdir>/owner-<id>.bin  records of {u64 logical, u64 generation, 16384 raw bytes}
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#ifdef __SSE4_2__
#include <nmmintrin.h>
#endif

#define NODESIZE 16384
#define STEP 4096

/* CRC-32C (Castagnoli), as used for btrfs metadata checksums: init 0xFFFFFFFF, final xor 0xFFFFFFFF.
 * Uses the SSE4.2 instruction when built with -msse4.2, otherwise a portable table (slower, same result). */
#ifdef __SSE4_2__
static uint32_t crc32c(const uint8_t *p, size_t n) {
    uint64_t c = 0xFFFFFFFFu;
    while (n >= 8) { uint64_t v; memcpy(&v, p, 8); c = _mm_crc32_u64(c, v); p += 8; n -= 8; }
    uint32_t c32 = (uint32_t)c;
    while (n--) c32 = _mm_crc32_u8(c32, *p++);
    return ~c32;
}
#else
static uint32_t crc_tab[256]; static int crc_ready;
static void crc_init(void) {
    for (uint32_t i = 0; i < 256; i++) { uint32_t c = i; for (int k = 0; k < 8; k++) c = (c & 1) ? (c >> 1) ^ 0x82F63B78u : (c >> 1); crc_tab[i] = c; }
    crc_ready = 1;
}
static uint32_t crc32c(const uint8_t *p, size_t n) {
    if (!crc_ready) crc_init();
    uint32_t c = 0xFFFFFFFFu;
    while (n--) c = crc_tab[(c ^ *p++) & 0xFF] ^ (c >> 8);
    return ~c;
}
#endif
static uint64_t le64(const uint8_t *p) { uint64_t v; memcpy(&v, p, 8); return v; }
static uint32_t le32(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }

typedef struct { uint64_t owner, leaves, nodes, genmin, genmax, misaligned; uint64_t hist[16]; } ostat;
#define MAXOWN 4096
static uint64_t own_id[MAXOWN]; static ostat own_st[MAXOWN]; static int nown = 0;
static ostat *get_owner(uint64_t id) {
    for (int i = 0; i < nown; i++) if (own_id[i] == id) return &own_st[i];
    if (nown >= MAXOWN) return &own_st[MAXOWN - 1];
    own_id[nown] = id; memset(&own_st[nown], 0, sizeof(ostat)); own_st[nown].genmin = UINT64_MAX;
    return &own_st[nown++];
}

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s imgdir outdir [owner ...]\n", argv[0]); return 2; }
    const char *imgdir = argv[1], *outdir = argv[2];
    uint64_t want[64]; FILE *wf[64]; int nwant = 0;
    for (int i = 3; i < argc && nwant < 64; i++) {
        want[nwant] = strtoull(argv[i], NULL, 10);
        char fn[1024]; snprintf(fn, sizeof fn, "%s/owner-%llu.bin", outdir, (unsigned long long)want[nwant]);
        wf[nwant] = fopen(fn, "wb"); if (!wf[nwant]) { perror(fn); return 1; }
        nwant++;
    }
    char path[1024];
    snprintf(path, sizeof path, "%s/dev-first-1MiB.img", imgdir);
    FILE *f = fopen(path, "rb"); if (!f) { perror(path); return 1; }
    uint8_t sb[4096]; fseek(f, 65536, SEEK_SET);
    if (fread(sb, 1, sizeof sb, f) != sizeof sb) { fprintf(stderr, "short superblock read\n"); return 1; }
    fclose(f);
    if (memcmp(sb + 64, "_BHRfS_M", 8) != 0) { fprintf(stderr, "superblock magic not found\n"); return 1; }
    uint8_t fsid[16]; memcpy(fsid, sb + 32, 16);
    fprintf(stderr, "fsid: "); for (int i = 0; i < 16; i++) fprintf(stderr, "%02x%s", fsid[i], (i==3||i==5||i==7||i==9)?"-":""); fprintf(stderr, "\n");

    snprintf(path, sizeof path, "%s/blocks.tsv", outdir);
    FILE *tsv = fopen(path, "w"); if (!tsv) { perror(path); return 1; }
    snprintf(path, sizeof path, "%s/manifest.txt", imgdir);
    FILE *mf = fopen(path, "r"); if (!mf) { perror(path); return 1; }

    char line[2048]; int mline = 0; uint64_t total_valid = 0;
    while (fgets(line, sizeof line, mf)) {
        unsigned long long logical, len, devoff; char type[128], fname[1024];
        if (sscanf(line, "%llu %llu %127s %llu %1023s", &logical, &len, type, &devoff, fname) != 5) continue;
        mline++;
        if (snprintf(path, sizeof path, "%s/%s", imgdir, fname) >= (int)sizeof path) { fprintf(stderr, "path too long\n"); return 1; }
        FILE *img = fopen(path, "rb"); if (!img) { perror(path); return 1; }
        uint8_t *buf = malloc(len); if (!buf) { fprintf(stderr, "oom\n"); return 1; }
        if (fread(buf, 1, len, img) != len) { fprintf(stderr, "short read %s\n", path); return 1; }
        fclose(img);
        uint64_t valid_here = 0;
        for (uint64_t p = 0; p + NODESIZE <= len; p += STEP) {
            const uint8_t *b = buf + p;
            if (memcmp(b + 32, fsid, 16) != 0) continue;
            if (le64(b + 48) != logical + p) continue;
            if (crc32c(b + 32, NODESIZE - 32) != le32(b)) continue;
            uint64_t gen = le64(b + 80), owner = le64(b + 88); uint32_t nr = le32(b + 96); int level = b[100];
            valid_here++; total_valid++;
            fprintf(tsv, "%llu\t%llu\t%llu\t%d\t%u\t%d\t%llu\n", (unsigned long long)(logical + p), (unsigned long long)gen,
                    (unsigned long long)owner, level, nr, mline, (unsigned long long)p);
            ostat *s = get_owner(owner);
            if (level == 0) s->leaves++; else s->nodes++;
            if (gen < s->genmin) s->genmin = gen;
            if (gen > s->genmax) s->genmax = gen;
            if (p % NODESIZE) s->misaligned++;
            for (int w = 0; w < nwant; w++) if (want[w] == owner) {
                uint64_t hdr[2] = { logical + p, gen };
                fwrite(hdr, 8, 2, wf[w]); fwrite(b, 1, NODESIZE, wf[w]);
            }
        }
        fprintf(stderr, "%-44s base=%llu len=%llu type=%s -> %llu intact tree blocks\n", fname,
                (unsigned long long)logical, (unsigned long long)len, type, (unsigned long long)valid_here);
        free(buf);
    }
    fclose(tsv); fclose(mf);
    for (int w = 0; w < nwant; w++) fclose(wf[w]);
    printf("total intact tree blocks: %llu\n", (unsigned long long)total_valid);
    printf("%-10s %10s %10s %14s %14s %10s\n", "owner", "leaves", "nodes", "gen_min", "gen_max", "!16K-aln");
    for (int i = 0; i < nown; i++)
        printf("%-10llu %10llu %10llu %14llu %14llu %10llu\n", (unsigned long long)own_id[i], (unsigned long long)own_st[i].leaves,
               (unsigned long long)own_st[i].nodes, (unsigned long long)own_st[i].genmin, (unsigned long long)own_st[i].genmax,
               (unsigned long long)own_st[i].misaligned);
    return 0;
}
