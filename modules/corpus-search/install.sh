#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_STAGE:?VANTA_MODULE_STAGE is required}"
: "${VANTA_MODULE_INSTALL_DIR:?VANTA_MODULE_INSTALL_DIR is required}"
: "${VANTA_MODULE_CURRENT_LINK:?VANTA_MODULE_CURRENT_LINK is required}"
: "${VANTA_MODULE_DATA_DIR:?VANTA_MODULE_DATA_DIR is required}"

for file in server.py corpus.py query.py taxonomy.py provision.py module.json profiles/small-arxiv.json profiles/medium-arxiv.json profiles/large-arxiv.json; do
	test -f "$VANTA_MODULE_STAGE/$file"
done
for command in python3 sqlite3; do
	command -v "$command" >/dev/null 2>&1
done

export PYTHONDONTWRITEBYTECODE=1
# FTS rebuild and ANALYZE spill a file the size of the index. Without this they land in /var/tmp on the
# root filesystem, which on an SBC is a fraction of the storage volume holding the corpus.
export SQLITE_TMPDIR="$VANTA_MODULE_DATA_DIR/tmp"
export TMPDIR="$SQLITE_TMPDIR"
mkdir -p "$SQLITE_TMPDIR"
python3 -c "import sqlite3; assert sqlite3.connect(':memory:').execute(\"select sqlite_compileoption_used('ENABLE_FTS5')\").fetchone()[0] == 1"
python3 "$VANTA_MODULE_STAGE/provision.py" \
	--data-dir "$VANTA_MODULE_DATA_DIR" \
	--profile-dir "$VANTA_MODULE_STAGE/profiles"
# SQLite unlinks its own spill files; this only clears anything left by an interrupted run.
rm -rf "$SQLITE_TMPDIR"

parent=$(dirname "$VANTA_MODULE_INSTALL_DIR")
mkdir -p "$parent"
tmp="$VANTA_MODULE_INSTALL_DIR.tmp.$$"
trap 'rm -rf "$tmp"' EXIT
rm -rf "$tmp"
mkdir -p "$tmp"
cp -a "$VANTA_MODULE_STAGE/." "$tmp/"
VANTA_MODULE_DATA_DIR="$VANTA_MODULE_DATA_DIR" python3 "$tmp/server.py" --self-test
rm -rf "$VANTA_MODULE_INSTALL_DIR"
mv "$tmp" "$VANTA_MODULE_INSTALL_DIR"
ln -sfn "$(basename "$VANTA_MODULE_INSTALL_DIR")" "$VANTA_MODULE_CURRENT_LINK"
trap - EXIT