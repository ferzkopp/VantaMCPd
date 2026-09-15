#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_INSTALL_DIR:?VANTA_MODULE_INSTALL_DIR is required}"
: "${VANTA_MODULE_CURRENT_LINK:?VANTA_MODULE_CURRENT_LINK is required}"

rm -f -- /run/vantamcpd-browser/browser.sock /etc/vantamcpd/browser-retrieval.env
if [ -L "$VANTA_MODULE_CURRENT_LINK" ] && [ "$(readlink "$VANTA_MODULE_CURRENT_LINK")" = "$(basename "$VANTA_MODULE_INSTALL_DIR")" ]; then
	rm -f "$VANTA_MODULE_CURRENT_LINK"
fi
rm -rf "$VANTA_MODULE_INSTALL_DIR"
if id vantamcpd-browser >/dev/null 2>&1; then
	userdel vantamcpd-browser 2>/dev/null || true
fi
if getent group vantamcpd-browser >/dev/null 2>&1; then
	groupdel vantamcpd-browser 2>/dev/null || true
fi
