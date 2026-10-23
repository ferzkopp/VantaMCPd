#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_STAGE:?VANTA_MODULE_STAGE is required}"
: "${VANTA_MODULE_INSTALL_DIR:?VANTA_MODULE_INSTALL_DIR is required}"
: "${VANTA_MODULE_CURRENT_LINK:?VANTA_MODULE_CURRENT_LINK is required}"
: "${VANTA_MODULE_RUN_AS:?VANTA_MODULE_RUN_AS is required}"

assets="${VANTA_MODULE_OPTION_ASSETS:-[\"hdri\",\"textures\",\"models\"]}"
service_user=vantamcpd-render
state_dir=/var/lib/vantamcpd-render
dropin_dir=/etc/systemd/system/vantamcpd-image-render.service.d

clamp() {
	if [ "$1" -lt "$2" ]; then echo "$2"; elif [ "$1" -gt "$3" ]; then echo "$3"; else echo "$1"; fi
}

progress() {
	printf 'VANTA_PROGRESS {"phase":"%s","current":%s,"total":%s,"unit":"steps","message":"%s"}\n' "$1" "$2" "$3" "$4"
}

for file in module.json server.py service.py sandbox.py schemas.py scene.py library.py assets.py gltf_mesh2.py polyhaven.py assets.json artifact_io.py artifact_protocol.py povray.conf povray.ini benchmark.ini image-render.service; do
	test -f "$VANTA_MODULE_STAGE/$file"
done
for command in bash python3 bwrap prlimit systemctl runuser useradd apt-get dpkg-deb dpkg-query tar; do
	command -v "$command" >/dev/null 2>&1
done
id "$VANTA_MODULE_RUN_AS" >/dev/null 2>&1
asset_selection=$(python3 -B "$VANTA_MODULE_STAGE/assets.py" --normalize "$assets")

export PYTHONDONTWRITEBYTECODE=1
export DEBIAN_FRONTEND=noninteractive
apt_flags=(-y -q --no-install-recommends -o Dpkg::Use-Pty=0 -o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

tmp="$VANTA_MODULE_INSTALL_DIR.tmp.$$"
downloads=$(mktemp -d /tmp/vanta-image-render.XXXXXX)
install -d -m 0755 /etc/vantamcpd "$dropin_dir"
env_tmp=$(mktemp /etc/vantamcpd/.image-render.XXXXXX)
dropin_tmp=$(mktemp "$dropin_dir/.limits.XXXXXX")
trap 'rm -f "$env_tmp" "$dropin_tmp"; rm -rf "$tmp" "$downloads"' EXIT

progress packages 0 7 "Refreshing package indexes"
env DEBIAN_FRONTEND=noninteractive apt-get update -q -o Dpkg::Use-Pty=0 -o DPkg::Lock::Timeout=300 </dev/null
progress packages 1 7 "Installing POV-Ray and its include files"
env DEBIAN_FRONTEND=noninteractive apt-get install "${apt_flags[@]}" povray povray-includes fonts-povray </dev/null
test -x /usr/bin/povray
test -f /usr/share/povray-3.7/include/colors.inc

parent=$(dirname "$VANTA_MODULE_INSTALL_DIR")
mkdir -p "$parent"
rm -rf "$tmp"
mkdir -p "$tmp"
cp -a "$VANTA_MODULE_STAGE/." "$tmp/"
install -d -m 0755 "$tmp/library" "$tmp/library/docs" "$tmp/library/examples"

progress library 2 7 "Extracting the POV-Ray manual and example scenes"
# Extracted privately so dpkg path-exclude rules for /usr/share/doc cannot drop the manual or examples.
(cd "$downloads" && apt-get download -q povray-doc povray-examples </dev/null)
for package in povray-doc povray-examples; do
	deb=$(find "$downloads" -maxdepth 1 -name "${package}_*.deb" -print -quit)
	test -n "$deb"
	dpkg-deb -x "$deb" "$downloads/root"
done
cp "$downloads"/root/usr/share/doc/povray/html/*.html "$tmp/library/docs/"
cp -R "$downloads/root/usr/share/doc/povray/examples/." "$tmp/library/examples/"
rm -rf "$tmp/library/examples/previews"
find "$tmp/library" -type l -delete
cp "$downloads/root/usr/share/doc/povray-doc/copyright" "$tmp/library/COPYRIGHT"

progress assets 3 7 "Fetching pinned CC0 asset packs and converting models (${asset_selection//\"/})"
previous_assets=""
if [ -d "$VANTA_MODULE_CURRENT_LINK/library/assets" ]; then previous_assets="$VANTA_MODULE_CURRENT_LINK/library/assets"; fi
python3 -B "$tmp/assets.py" --fetch "$asset_selection" "$tmp/library/assets" ${previous_assets:+"$previous_assets"}
# Optional: without the index, render_polyhaven reports that it is unavailable and the starter set still works.
python3 -B "$tmp/polyhaven.py" --index "$tmp/library"

progress library 4 7 "Indexing documentation, examples, includes and assets"
python3 -B "$tmp/library.py" --build "$tmp/library"
povray_version=$(dpkg-query -W -f='${Version}' povray)
printf '{"povray":"%s","assets":%s}\n' "$povray_version" "$asset_selection" > "$tmp/environment.json"
chmod -R a+rX "$tmp"

progress environment 5 7 "Preparing the service account and resource limits"
if ! id "$service_user" >/dev/null 2>&1; then
	useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin "$service_user"
fi
caller_uid=$(id -u "$VANTA_MODULE_RUN_AS")
caller_gid=$(id -g "$VANTA_MODULE_RUN_AS")
caller_group=$(id -gn "$VANTA_MODULE_RUN_AS")
usermod -a -G "$caller_group" "$service_user"

mem_total_mb=$(awk '/^MemTotal:/ {printf "%d", $2 / 1024}' /proc/meminfo)
cores=$(nproc)
cgroup_high_mb=$(clamp $((mem_total_mb * 75 / 100)) 512 65536)
cgroup_max_mb=$(clamp $((mem_total_mb * 85 / 100)) 640 65536)
threads=$(clamp "${VANTA_MODULE_OPTION_THREADS:-$cores}" 1 64)
concurrent_renders=$(clamp "${VANTA_MODULE_OPTION_CONCURRENT_RENDERS:-1}" 1 4)
memory_mb=$(clamp "${VANTA_MODULE_OPTION_MAX_MEMORY_MB:-$((cgroup_high_mb / concurrent_renders))}" 256 16384)
timeout_ms=$(clamp "${VANTA_MODULE_OPTION_MAX_TIMEOUT_MS:-600000}" 10000 1800000)
background_timeout_ms=$(clamp "${VANTA_MODULE_OPTION_MAX_BACKGROUND_TIMEOUT_MS:-14400000}" 60000 86400000)
calls_per_minute=$(clamp "${VANTA_MODULE_OPTION_CALLS_PER_MINUTE:-20}" 1 120)
cpu_quota=$((cores * 100))
tasks_max=$(((threads + 48) * (concurrent_renders + 1) + 32))

{
	printf 'VANTA_RENDER_CALLER_UID=%s\n' "$caller_uid"
	printf 'VANTA_RENDER_CLIENT_GID=%s\n' "$caller_gid"
	printf 'VANTA_RENDER_MEMORY_MB=%s\n' "$memory_mb"
	printf 'VANTA_RENDER_THREADS=%s\n' "$threads"
	printf 'VANTA_RENDER_MAX_TIMEOUT_MS=%s\n' "$timeout_ms"
	printf 'VANTA_RENDER_MAX_BACKGROUND_TIMEOUT_MS=%s\n' "$background_timeout_ms"
	printf 'VANTA_RENDER_CONCURRENT_RENDERS=%s\n' "$concurrent_renders"
	printf 'VANTA_RENDER_CALLS_PER_MINUTE=%s\n' "$calls_per_minute"
	if [ -n "${VANTA_ARTIFACT_ROOT:-}" ]; then
		printf 'VANTA_ARTIFACT_ROOT=%s\n' "$VANTA_ARTIFACT_ROOT"
		printf 'VANTA_ARTIFACT_PROTOCOL_VERSION=%s\n' "${VANTA_ARTIFACT_PROTOCOL_VERSION:-1}"
	fi
} > "$env_tmp"
chmod 0644 "$env_tmp"
mv -f "$env_tmp" /etc/vantamcpd/image-render.env

{
	printf '# Generated by the VantaMCPd image-render installer from this node.\n[Service]\n'
	printf 'MemoryHigh=%sM\nMemoryMax=%sM\nCPUQuota=%s%%\nTasksMax=%s\n' "$cgroup_high_mb" "$cgroup_max_mb" "$cpu_quota" "$tasks_max"
	if [ -n "${VANTA_ARTIFACT_ROOT:-}" ]; then
		printf 'Group=%s\nReadWritePaths=%s\n' "$caller_group" "$VANTA_ARTIFACT_ROOT"
	fi
} > "$dropin_tmp"
chmod 0644 "$dropin_tmp"
mv -f "$dropin_tmp" "$dropin_dir/limits.conf"
install -d -m 0700 -o "$service_user" -g "$service_user" "$state_dir" "$state_dir/calls"
# Kept across updates so a recorded benchmark survives; uninstall removes it.
install -d -m 0755 /var/lib/vantamcpd/module-reports
install -d -m 0755 -o "$service_user" -g "$service_user" /var/lib/vantamcpd/module-reports/image-render

progress verify 6 7 "Checking schemas, library, sandbox and POV-Ray I/O restrictions"
python3 -B "$tmp/schemas.py"
python3 -B "$tmp/scene.py"
python3 -B "$tmp/artifact_io.py"
python3 -B "$tmp/library.py"
python3 -B "$tmp/assets.py"
python3 -B "$tmp/gltf_mesh2.py"
python3 -B "$tmp/polyhaven.py"
python3 -B "$tmp/sandbox.py"
VANTA_RENDER_INSTALL_DIR="$tmp" VANTA_RENDER_STATE_DIR="$state_dir" python3 -B "$tmp/service.py" --self-test
python3 -B "$tmp/server.py" --self-test
runuser -u "$service_user" -- env PYTHONDONTWRITEBYTECODE=1 VANTA_RENDER_INSTALL_DIR="$tmp" VANTA_RENDER_STATE_DIR="$state_dir" \
	VANTA_RENDER_MEMORY_MB="$memory_mb" VANTA_RENDER_THREADS="$threads" python3 -B "$tmp/sandbox.py" --smoke-test

rm -rf "$VANTA_MODULE_INSTALL_DIR"
mv "$tmp" "$VANTA_MODULE_INSTALL_DIR"
ln -sfn "$(basename "$VANTA_MODULE_INSTALL_DIR")" "$VANTA_MODULE_CURRENT_LINK"
trap - EXIT
rm -f "$env_tmp" "$dropin_tmp" 2>/dev/null || true
rm -rf "$downloads"
progress complete 7 7 "Image Render is ready"
