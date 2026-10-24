#!/bin/bash
# Demonstrates the SynoCommunity spksrc "Erase all of the package data files" bug (mk/spksrc.service/installer.dsm7, postuninst)
# and checks the proposed fix. See docs/THE-BUG.md.
# SAFE: everything runs inside a throw-away temp directory; the "dangerous" find only ever sees that directory.
# Usage: bash postuninst-delete-test.sh
set -u
install_log() { cat; }

# --- ORIGINAL code, verbatim from mk/spksrc.service/installer.dsm7 (postuninst) ---------------------------------
original() {
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
}

# --- PROPOSED fix --------------------------------------------------------------------------------------------------
fixed() {
    if [ "$wizard_delete_data" = "true" ]; then
        echo "Removing files..." | install_log
        _etc_path=$(realpath "/var/packages/${SYNOPKG_PKGNAME}/etc" 2>/dev/null)
        for _dir in "${SYNOPKG_PKGHOME}" "${SYNOPKG_PKGVAR}" "${_etc_path}"; do
            # An empty or missing path must never reach ls/find: with an empty unquoted variable, `ls -A` lists and
            # `find -mindepth 1 -delete` deletes the *current directory*.
            if [ -z "${_dir}" ] || [ ! -d "${_dir}" ]; then
                echo "Skipping '${_dir}': not an existing directory" | install_log
                continue
            fi
            if [ "$(realpath "${_dir}")" = "/" ]; then
                echo "Refusing to erase '/'" | install_log
                continue
            fi
            if [ "$(ls -A "${_dir}")" != "" ]; then
                find "${_dir}" -mindepth 1 -delete | install_log
            fi
        done
    fi
}

run_case() {  # $1=function  $2=label  $3=PKGHOME  $4=PKGVAR
    local SB; SB=$(mktemp -d); mkdir -p "$SB/root/volume1/other share" "$SB/root/volume1/@appdata/mypkg" "$SB/root/volume1/@apphome/mypkg" "$SB/root/etc"
    touch "$SB/root/volume1/other share/PRECIOUS-1" "$SB/root/volume1/other share/PRECIOUS-2" "$SB/root/etc/passwd" "$SB/root/volume1/@appdata/mypkg/data.db" "$SB/root/volume1/@apphome/mypkg/home.cfg"
    export wizard_delete_data=true SYNOPKG_PKGNAME=no_such_pkg_dangling   # /var/packages/no_such_pkg_dangling/etc does not exist
    export SYNOPKG_PKGHOME="${3//@SB@/$SB}" SYNOPKG_PKGVAR="${4//@SB@/$SB}"
    ( cd "$SB/root" && $1 >/dev/null 2>&1 )          # postuninst runs with an arbitrary current directory (here: the fake "/")
    local left; left=$(cd "$SB/root" && find . -type f | wc -l)
    local precious; precious=$(ls "$SB/root/volume1/other share" 2>/dev/null | wc -l)
    printf '  %-9s %-52s files left: %2s   "other share" files kept: %s/2\n' "$1" "$2" "$left" "$precious"
    rm -rf "$SB"
}
echo "Fake tree: 5 files (2 in an unrelated share, 1 in /etc, 2 package files). Package etc dir is dangling (realpath fails)."
echo "Scenario 1: package home+var exist, package etc is dangling  (what happened)"
run_case original "(erases everything under the cwd!)" "@SB@/root/volume1/@apphome/mypkg" "@SB@/root/volume1/@appdata/mypkg"
run_case fixed    "(erases only the package's own dirs)" "@SB@/root/volume1/@apphome/mypkg" "@SB@/root/volume1/@appdata/mypkg"
echo "Scenario 2: ALL package dirs missing (volume rebuilt)  -> nothing must be deleted"
run_case original "(erases everything under the cwd!)" "@SB@/root/nope1" "@SB@/root/nope2"
run_case fixed    "(deletes nothing)" "@SB@/root/nope1" "@SB@/root/nope2"
echo "Scenario 3: SYNOPKG_PKGHOME / SYNOPKG_PKGVAR empty strings"
run_case original "(erases everything under the cwd!)" "" ""
run_case fixed    "(deletes nothing)" "" ""
