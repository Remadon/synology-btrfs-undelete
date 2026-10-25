# The bug that caused it

**Where:** `mk/spksrc.service/installer.dsm7` in [SynoCommunity/spksrc](https://github.com/SynoCommunity/spksrc), function `postuninst`.
**Who is affected:** every package that uses that installer (the repository has about 175 package folders; I have not audited which of them reach this code path), when the user answers the uninstall dialog with
**"Erase all of the package data files. (Not Recoverable)"** (`wizard_delete_data=true`) and the package's `etc` directory is
missing or dangling (for example because the volume it was installed on was rebuilt or removed, so the package shows as "broken").

**Upstream report:** [SynoCommunity/spksrc#7650](https://github.com/SynoCommunity/spksrc/issues/7650)

## The code

```sh
    if [ "$wizard_delete_data" = "true" ]; then
        echo "Removing files..." | install_log
        if [ "$(ls -A ${SYNOPKG_PKGHOME})" != "" ]; then
            find ${SYNOPKG_PKGHOME} -mindepth 1 -delete | install_log
        fi

        if [ "$(ls -A ${SYNOPKG_PKGVAR})" != "" ]; then
            find ${SYNOPKG_PKGVAR} -mindepth 1 -delete | install_log
        fi

        _etc_path=$(realpath /var/packages/${SYNOPKG_PKGNAME}/etc)
        if [ "$(ls -A ${_etc_path})" != "" ]; then
            find ${_etc_path} -mindepth 1 -delete | install_log
        fi
    fi
```

If `/var/packages/<pkg>/etc` is dangling, `realpath` fails and `_etc_path` is the empty string. Because the variable is unquoted:

1. `ls -A ${_etc_path}` becomes `ls -A`, which lists the **current directory** (here `/`). Non-empty, so the guard passes.
2. `find ${_etc_path} -mindepth 1 -delete` becomes `find -mindepth 1 -delete`. With no starting point GNU `find` uses `.`, so it
   deletes everything under the current directory that the script's user may delete.

The same happens for `SYNOPKG_PKGHOME` / `SYNOPKG_PKGVAR` if either is empty.

## What it looked like in the log

```
Removing files...
ls: cannot access '/volume1/@apphome/syncthing': No such file or directory
ls: cannot access '/volume1/@appdata/syncthing': No such file or directory
realpath: /var/packages/syncthing/etc: No such file or directory
find: cannot delete `./run/scemd_system_status': Permission denied
...                                   (hundreds of thousands of lines: ./proc/..., ./sys/..., ./usr/...)
End postuninst ret=[0]
```

The relative `./...` paths starting at the filesystem root are the tell-tale. The script ran as the package's unprivileged service
account, so the system itself survived with "Permission denied" everywhere; but that account had read/write on the user's
main share (so Syncthing could sync a folder in it), and everything it could write was erased, silently.

## Demonstration (safe)

`scripts/postuninst-bug-demo.sh` runs the original code and a fixed version in a throw-away directory and shows the original
erasing a whole fake tree in three scenarios (dangling etc, all package dirs missing, empty variables).

## Fix

```diff
-        if [ "$(ls -A ${SYNOPKG_PKGHOME})" != "" ]; then
-            find ${SYNOPKG_PKGHOME} -mindepth 1 -delete | install_log
-        fi
-
-        if [ "$(ls -A ${SYNOPKG_PKGVAR})" != "" ]; then
-            find ${SYNOPKG_PKGVAR} -mindepth 1 -delete | install_log
-        fi
-
-        _etc_path=$(realpath /var/packages/${SYNOPKG_PKGNAME}/etc)
-        if [ "$(ls -A ${_etc_path})" != "" ]; then
-            find ${_etc_path} -mindepth 1 -delete | install_log
-        fi
+        _etc_path=$(realpath "/var/packages/${SYNOPKG_PKGNAME}/etc" 2>/dev/null)
+        for _dir in "${SYNOPKG_PKGHOME}" "${SYNOPKG_PKGVAR}" "${_etc_path}"; do
+            if [ -z "${_dir}" ] || [ ! -d "${_dir}" ]; then
+                echo "Skipping '${_dir}': not an existing directory" | install_log
+                continue
+            fi
+            if [ "$(realpath "${_dir}")" = "/" ]; then
+                echo "Refusing to erase '/'" | install_log
+                continue
+            fi
+            if [ "$(ls -A "${_dir}")" != "" ]; then
+                find "${_dir}" -mindepth 1 -delete | install_log
+            fi
+        done
```

## What to do as a user until it is fixed

* Do not choose "Erase all of the package data files" for a package that shows as broken, or after you rebuilt/moved/removed the
  volume it was installed on. Choose "Uninstall only" and delete leftovers by hand.
* Check which shares your `sc-*` package accounts can write to: Control Panel > Shared Folder > edit a share > Permissions, then pick **System internal user** in the dropdown (package accounts do not appear under User & Group). Remove access they don't need; Read-only cannot delete anything. SynoCommunity's [permissions guide](https://docs.synocommunity.com/user-guide/permissions/) explains how these accounts work.
* Take snapshots (Snapshot Replication) and keep a backup elsewhere.
