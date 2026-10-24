CC     ?= gcc
CFLAGS ?= -O2 -Wall -Wextra

all: scanmeta extent-restore

# hardware CRC32C when the CPU has SSE4.2, portable table otherwise (same result, slower)
scanmeta: src/scanmeta.c
	$(CC) $(CFLAGS) -msse4.2 -o $@ $< 2>/dev/null || $(CC) $(CFLAGS) -o $@ $<

extent-restore: src/extent-restore.c
	$(CC) $(CFLAGS) -pthread -o $@ $<

# fully static binary for a NAS with an old libc; refresh the checksum afterwards
static: src/extent-restore.c
	$(CC) $(CFLAGS) -static -pthread -o bin/extent-restore-x86_64-linux-static $<
	cd bin && sha256sum extent-restore-x86_64-linux-static > SHA256SUMS

test: extent-restore
	python3 tests/test_extent_restore.py ./extent-restore

clean:
	rm -f scanmeta extent-restore
.PHONY: all static test clean
