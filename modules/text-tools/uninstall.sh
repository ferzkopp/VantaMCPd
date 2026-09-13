#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_INSTALL_DIR:?VANTA_MODULE_INSTALL_DIR is required}"
: "${VANTA_MODULE_CURRENT_LINK:?VANTA_MODULE_CURRENT_LINK is required}"

if [ -L "$VANTA_MODULE_CURRENT_LINK" ]; then
  target=$(readlink "$VANTA_MODULE_CURRENT_LINK")
  if [ "$target" = "$(basename "$VANTA_MODULE_INSTALL_DIR")" ]; then
    rm -f "$VANTA_MODULE_CURRENT_LINK"
  fi
fi
rm -rf "$VANTA_MODULE_INSTALL_DIR"