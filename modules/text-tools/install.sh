#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_STAGE:?VANTA_MODULE_STAGE is required}"
: "${VANTA_MODULE_INSTALL_DIR:?VANTA_MODULE_INSTALL_DIR is required}"
: "${VANTA_MODULE_CURRENT_LINK:?VANTA_MODULE_CURRENT_LINK is required}"

test -f "$VANTA_MODULE_STAGE/server.py"
test -f "$VANTA_MODULE_STAGE/module.json"
test -f "$VANTA_MODULE_STAGE/operation_common.py"
test -f "$VANTA_MODULE_STAGE/operations.py"
test -f "$VANTA_MODULE_STAGE/operations_text.py"
test -f "$VANTA_MODULE_STAGE/operations_data.py"
test -f "$VANTA_MODULE_STAGE/operations_developer.py"
test -f "$VANTA_MODULE_STAGE/artifact_io.py"
test -f "$VANTA_MODULE_STAGE/artifact_protocol.py"
for command in python3 rg jq awk sed; do
	command -v "$command" >/dev/null 2>&1
done

parent=$(dirname "$VANTA_MODULE_INSTALL_DIR")
mkdir -p "$parent"
tmp="$VANTA_MODULE_INSTALL_DIR.tmp.$$"
trap 'rm -rf "$tmp"' EXIT
rm -rf "$tmp"
mkdir -p "$tmp"
cp -a "$VANTA_MODULE_STAGE/." "$tmp/"
PYTHONDONTWRITEBYTECODE=1 python3 "$tmp/artifact_protocol.py"
python3 "$tmp/server.py" --self-test
rm -rf "$VANTA_MODULE_INSTALL_DIR"
mv "$tmp" "$VANTA_MODULE_INSTALL_DIR"
ln -sfn "$(basename "$VANTA_MODULE_INSTALL_DIR")" "$VANTA_MODULE_CURRENT_LINK"
trap - EXIT