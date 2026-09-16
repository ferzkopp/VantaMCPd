#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_STAGE:?VANTA_MODULE_STAGE is required}"
: "${VANTA_MODULE_INSTALL_DIR:?VANTA_MODULE_INSTALL_DIR is required}"
: "${VANTA_MODULE_CURRENT_LINK:?VANTA_MODULE_CURRENT_LINK is required}"
: "${VANTA_MODULE_RUN_AS:?VANTA_MODULE_RUN_AS is required}"
: "${VANTA_MODULE_DATA_DIR:?VANTA_MODULE_DATA_DIR is required}"

for file in module.json server.py service.py store.py artifact-storage.service; do
	test -f "$VANTA_MODULE_STAGE/$file"
done
for command in bash python3 systemctl useradd; do
	command -v "$command" >/dev/null 2>&1
done

service_user=vantamcpd-artifacts
if ! id "$service_user" >/dev/null 2>&1; then
	useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin "$service_user"
fi
caller_uid=$(id -u "$VANTA_MODULE_RUN_AS")
caller_gid=$(id -g "$VANTA_MODULE_RUN_AS")
caller_group=$(id -gn "$VANTA_MODULE_RUN_AS")
usermod -a -G "$caller_group" "$service_user"

install -d -m 2770 -o "$service_user" -g "$caller_group" "$VANTA_MODULE_DATA_DIR"
chmod 2770 "$VANTA_MODULE_DATA_DIR"

install -d -m 0755 /etc/vantamcpd
env_tmp=$(mktemp /etc/vantamcpd/.artifact-storage.XXXXXX)
tmp="$VANTA_MODULE_INSTALL_DIR.tmp.$$"
trap 'rm -f "$env_tmp"; rm -rf "$tmp"' EXIT
{
	printf 'VANTA_ARTIFACT_CALLER_UID=%s\n' "$caller_uid"
	printf 'VANTA_ARTIFACT_CALLER_GID=%s\n' "$caller_gid"
	printf 'VANTA_ARTIFACT_ROOT=%s\n' "$VANTA_MODULE_DATA_DIR"
	printf 'VANTA_ARTIFACT_TOTAL_QUOTA_MB=%s\n' "${VANTA_MODULE_OPTION_TOTAL_QUOTA_MB:-10240}"
	printf 'VANTA_ARTIFACT_PRODUCER_QUOTA_MB=%s\n' "${VANTA_MODULE_OPTION_PRODUCER_QUOTA_MB:-2048}"
	printf 'VANTA_ARTIFACT_MAX_ARTIFACT_MB=%s\n' "${VANTA_MODULE_OPTION_MAX_ARTIFACT_MB:-512}"
	printf 'VANTA_ARTIFACT_RETENTION_DAYS=%s\n' "${VANTA_MODULE_OPTION_RETENTION_DAYS:-7}"
	printf 'VANTA_ARTIFACT_MAX_RETENTION_DAYS=%s\n' "${VANTA_MODULE_OPTION_MAX_RETENTION_DAYS:-90}"
	printf 'VANTA_ARTIFACT_GC_INTERVAL_MINUTES=%s\n' "${VANTA_MODULE_OPTION_GC_INTERVAL_MINUTES:-15}"
	printf 'VANTA_ARTIFACT_FREE_RESERVE_MB=%s\n' "${VANTA_MODULE_OPTION_FREE_RESERVE_MB:-512}"
} > "$env_tmp"
chmod 0640 "$env_tmp"
chown root:"$caller_group" "$env_tmp"
mv -f "$env_tmp" /etc/vantamcpd/artifact-storage.env

parent=$(dirname "$VANTA_MODULE_INSTALL_DIR")
mkdir -p "$parent"
rm -rf "$tmp"
mkdir -p "$tmp"
cp -a "$VANTA_MODULE_STAGE/." "$tmp/"
PYTHONDONTWRITEBYTECODE=1 python3 "$tmp/store.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$tmp/server.py" --self-test
rm -rf "$VANTA_MODULE_INSTALL_DIR"
mv "$tmp" "$VANTA_MODULE_INSTALL_DIR"
ln -sfn "$(basename "$VANTA_MODULE_INSTALL_DIR")" "$VANTA_MODULE_CURRENT_LINK"
trap - EXIT