#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_INSTALL_DIR:?VANTA_MODULE_INSTALL_DIR is required}"
: "${VANTA_MODULE_CURRENT_LINK:?VANTA_MODULE_CURRENT_LINK is required}"

rm -f -- /run/vantamcpd-image/image.sock /etc/vantamcpd/image-processing.env
rm -rf -- /etc/systemd/system/vantamcpd-image-processing.service.d
rm -rf -- /var/lib/vantamcpd-image
if [ -L "$VANTA_MODULE_CURRENT_LINK" ] && [ "$(readlink "$VANTA_MODULE_CURRENT_LINK")" = "$(basename "$VANTA_MODULE_INSTALL_DIR")" ]; then
	rm -f "$VANTA_MODULE_CURRENT_LINK"
fi
rm -rf "$VANTA_MODULE_INSTALL_DIR"
if id vantamcpd-image >/dev/null 2>&1; then
	userdel vantamcpd-image 2>/dev/null || true
fi
if getent group vantamcpd-image >/dev/null 2>&1; then
	groupdel vantamcpd-image 2>/dev/null || true
fi