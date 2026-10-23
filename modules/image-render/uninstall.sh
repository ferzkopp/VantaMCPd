#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_INSTALL_DIR:?VANTA_MODULE_INSTALL_DIR is required}"
: "${VANTA_MODULE_CURRENT_LINK:?VANTA_MODULE_CURRENT_LINK is required}"

rm -f -- /run/vantamcpd-render/render.sock /etc/vantamcpd/image-render.env
rm -rf -- /etc/systemd/system/vantamcpd-image-render.service.d
rm -rf -- /var/lib/vantamcpd-render /var/lib/vantamcpd/module-reports/image-render
if [ -L "$VANTA_MODULE_CURRENT_LINK" ] && [ "$(readlink "$VANTA_MODULE_CURRENT_LINK")" = "$(basename "$VANTA_MODULE_INSTALL_DIR")" ]; then
	rm -f "$VANTA_MODULE_CURRENT_LINK"
fi
rm -rf "$VANTA_MODULE_INSTALL_DIR"
if id vantamcpd-render >/dev/null 2>&1; then
	userdel vantamcpd-render 2>/dev/null || true
fi
if getent group vantamcpd-render >/dev/null 2>&1; then
	groupdel vantamcpd-render 2>/dev/null || true
fi
