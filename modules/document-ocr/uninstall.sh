#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_INSTALL_DIR:?}"
: "${VANTA_MODULE_CURRENT_LINK:?}"
: "${VANTA_MODULE_RUN_AS:?}"

state=/var/lib/vantamcpd-document-ocr
home=$(getent passwd "$VANTA_MODULE_RUN_AS" | cut -d: -f6)
uid=$(id -u "$VANTA_MODULE_RUN_AS")
if [ -d "/run/user/$uid" ]; then
    runuser -u "$VANTA_MODULE_RUN_AS" -- env HOME="$home" XDG_RUNTIME_DIR="/run/user/$uid" \
        podman image rm localhost/vantamcpd-document-ocr:0.1.0 || true
fi
if [ -L "$VANTA_MODULE_CURRENT_LINK" ] && [ "$(readlink "$VANTA_MODULE_CURRENT_LINK")" = "$(basename "$VANTA_MODULE_INSTALL_DIR")" ]; then
    rm -f -- "$VANTA_MODULE_CURRENT_LINK"
fi
rm -rf -- "$VANTA_MODULE_INSTALL_DIR" "$state"