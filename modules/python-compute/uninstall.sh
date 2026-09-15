#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_INSTALL_DIR:?VANTA_MODULE_INSTALL_DIR is required}"
: "${VANTA_MODULE_CURRENT_LINK:?VANTA_MODULE_CURRENT_LINK is required}"

rm -f -- /run/vantamcpd-python/python.sock /etc/vantamcpd/python-compute.env
rm -rf -- /etc/systemd/system/vantamcpd-python-compute.service.d
rm -rf -- /var/lib/vantamcpd-python
if [ -L "$VANTA_MODULE_CURRENT_LINK" ] && [ "$(readlink "$VANTA_MODULE_CURRENT_LINK")" = "$(basename "$VANTA_MODULE_INSTALL_DIR")" ]; then
	rm -f "$VANTA_MODULE_CURRENT_LINK"
fi
rm -rf "$VANTA_MODULE_INSTALL_DIR"
# The provisioned Python packages stay installed; they are ordinary distribution packages.
if id vantamcpd-python >/dev/null 2>&1; then
	userdel vantamcpd-python 2>/dev/null || true
fi
if getent group vantamcpd-python >/dev/null 2>&1; then
	groupdel vantamcpd-python 2>/dev/null || true
fi
