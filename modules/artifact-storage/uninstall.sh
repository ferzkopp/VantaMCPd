#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_INSTALL_DIR:?VANTA_MODULE_INSTALL_DIR is required}"
: "${VANTA_MODULE_CURRENT_LINK:?VANTA_MODULE_CURRENT_LINK is required}"

rm -f -- /run/vantamcpd-artifacts/artifacts.sock /etc/vantamcpd/artifact-storage.env
if [ -L "$VANTA_MODULE_CURRENT_LINK" ] && [ "$(readlink "$VANTA_MODULE_CURRENT_LINK")" = "$(basename "$VANTA_MODULE_INSTALL_DIR")" ]; then
	rm -f "$VANTA_MODULE_CURRENT_LINK"
fi
rm -rf "$VANTA_MODULE_INSTALL_DIR"
if id vantamcpd-artifacts >/dev/null 2>&1; then
	userdel vantamcpd-artifacts 2>/dev/null || true
fi
if getent group vantamcpd-artifacts >/dev/null 2>&1; then
	groupdel vantamcpd-artifacts 2>/dev/null || true
fi