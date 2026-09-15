#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_STAGE:?VANTA_MODULE_STAGE is required}"
: "${VANTA_MODULE_INSTALL_DIR:?VANTA_MODULE_INSTALL_DIR is required}"
: "${VANTA_MODULE_CURRENT_LINK:?VANTA_MODULE_CURRENT_LINK is required}"
: "${VANTA_MODULE_RUN_AS:?VANTA_MODULE_RUN_AS is required}"

for file in module.json server.py service.py browser.py network_policy.py extraction.py browser-retrieval.service; do
	test -f "$VANTA_MODULE_STAGE/$file"
done
for command in bash python3 chromium systemctl useradd runuser timeout; do
	command -v "$command" >/dev/null 2>&1
done
id "$VANTA_MODULE_RUN_AS" >/dev/null 2>&1

service_user=vantamcpd-browser
if ! id "$service_user" >/dev/null 2>&1; then
	useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin "$service_user"
fi
caller_uid=$(id -u "$VANTA_MODULE_RUN_AS")
caller_gid=$(id -g "$VANTA_MODULE_RUN_AS")
caller_group=$(id -gn "$VANTA_MODULE_RUN_AS")
usermod -a -G "$caller_group" "$service_user"

install -d -m 0755 /etc/vantamcpd
env_tmp=$(mktemp /etc/vantamcpd/.browser-retrieval.XXXXXX)
smoke_dir=$(mktemp -d /tmp/vantamcpd-browser-smoke.XXXXXX)
tmp="$VANTA_MODULE_INSTALL_DIR.tmp.$$"
trap 'rm -f "$env_tmp"; rm -rf "$smoke_dir" "$tmp"' EXIT
printf 'VANTA_BROWSER_CALLER_UID=%s\nVANTA_BROWSER_CALLER_GID=%s\n' "$caller_uid" "$caller_gid" > "$env_tmp"
chmod 0600 "$env_tmp"
mv -f "$env_tmp" /etc/vantamcpd/browser-retrieval.env

parent=$(dirname "$VANTA_MODULE_INSTALL_DIR")
mkdir -p "$parent"
rm -rf "$tmp"
mkdir -p "$tmp"
cp -a "$VANTA_MODULE_STAGE/." "$tmp/"
PYTHONDONTWRITEBYTECODE=1 python3 "$tmp/server.py" --self-test
PYTHONDONTWRITEBYTECODE=1 python3 "$tmp/browser.py"

chown -R "$service_user:$service_user" "$smoke_dir"
runuser -u "$service_user" -- env HOME="$smoke_dir" timeout 25s chromium \
	--headless=new --disable-gpu --disable-dev-shm-usage --no-first-run --no-default-browser-check \
	--user-data-dir="$smoke_dir/profile" --dump-dom 'data:text/html,<title>VantaBrowserSmoke</title><p>ready</p>' \
	> "$smoke_dir/output" 2> "$smoke_dir/error"
grep -q 'VantaBrowserSmoke' "$smoke_dir/output"

rm -rf "$VANTA_MODULE_INSTALL_DIR"
mv "$tmp" "$VANTA_MODULE_INSTALL_DIR"
ln -sfn "$(basename "$VANTA_MODULE_INSTALL_DIR")" "$VANTA_MODULE_CURRENT_LINK"
trap - EXIT
rm -rf "$smoke_dir"
