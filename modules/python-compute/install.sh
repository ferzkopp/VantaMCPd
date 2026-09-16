#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_STAGE:?VANTA_MODULE_STAGE is required}"
: "${VANTA_MODULE_INSTALL_DIR:?VANTA_MODULE_INSTALL_DIR is required}"
: "${VANTA_MODULE_CURRENT_LINK:?VANTA_MODULE_CURRENT_LINK is required}"
: "${VANTA_MODULE_RUN_AS:?VANTA_MODULE_RUN_AS is required}"

bundle="${VANTA_MODULE_OPTION_BUNDLE:-science}"
service_user=vantamcpd-python
state_dir=/var/lib/vantamcpd-python
dropin_dir=/etc/systemd/system/vantamcpd-python-compute.service.d

clamp() { # value minimum maximum
	if [ "$1" -lt "$2" ]; then echo "$2"; elif [ "$1" -gt "$3" ]; then echo "$3"; else echo "$1"; fi
}

progress() {
	printf 'VANTA_PROGRESS {"phase":"%s","current":%s,"total":%s,"unit":"steps","message":"%s"}\n' "$1" "$2" "$3" "$4"
}

for file in module.json server.py service.py sandbox.py runner.py schemas.py inventory.py artifact_io.py python-compute.service; do
	test -f "$VANTA_MODULE_STAGE/$file"
done
for command in bash python3 bwrap prlimit systemctl runuser useradd apt-get; do
	command -v "$command" >/dev/null 2>&1
done
id "$VANTA_MODULE_RUN_AS" >/dev/null 2>&1

export PYTHONDONTWRITEBYTECODE=1
export DEBIAN_FRONTEND=noninteractive
apt_flags=(-y -q --no-install-recommends -o Dpkg::Use-Pty=0 -o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

mapfile -t phases < <(python3 "$VANTA_MODULE_STAGE/inventory.py" --bundle "$bundle" --apt-plan)
total=$(( ${#phases[@]} + 4 ))
step=0

progress packages "$step" "$total" "Refreshing package indexes"
env DEBIAN_FRONTEND=noninteractive apt-get update -q -o Dpkg::Use-Pty=0 -o DPkg::Lock::Timeout=300 </dev/null
for phase in "${phases[@]}"; do
	step=$((step + 1))
	tier="${phase%% *}"
	packages="${phase#* }"
	progress packages "$step" "$total" "Installing the $tier package set"
	# shellcheck disable=SC2086
	env DEBIAN_FRONTEND=noninteractive apt-get install "${apt_flags[@]}" $packages </dev/null
done

step=$((step + 1))
progress environment "$step" "$total" "Preparing the service account and sandbox"
if ! id "$service_user" >/dev/null 2>&1; then
	useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin "$service_user"
fi
caller_uid=$(id -u "$VANTA_MODULE_RUN_AS")
caller_gid=$(id -g "$VANTA_MODULE_RUN_AS")
caller_group=$(id -gn "$VANTA_MODULE_RUN_AS")
# The broker must share a group with the caller to hand over the socket. Adding the service account to
# the caller's existing group keeps already-open SSH sessions working; the reverse would not.
usermod -a -G "$caller_group" "$service_user"

install -d -m 0755 /etc/vantamcpd
install -d -m 0755 "$dropin_dir"
env_tmp=$(mktemp /etc/vantamcpd/.python-compute.XXXXXX)
dropin_tmp=$(mktemp "$dropin_dir/.limits.XXXXXX")
tmp="$VANTA_MODULE_INSTALL_DIR.tmp.$$"
trap 'rm -f "$env_tmp" "$dropin_tmp"; rm -rf "$tmp"' EXIT

# Limits are sized from this node so a larger worker is not held to a 1 GB board's budget. The cgroup
# caps must move with the per-call limits, or the service is killed long before a call reaches its own.
mem_total_mb=$(awk '/^MemTotal:/ {printf "%d", $2 / 1024}' /proc/meminfo)
cores=$(nproc)
cgroup_high_mb=$(clamp $((mem_total_mb * 60 / 100)) 256 65536)
cgroup_max_mb=$(clamp $((mem_total_mb * 75 / 100)) 320 65536)
if [ "$cores" -ge 4 ]; then derived_concurrency=2; else derived_concurrency=1; fi
concurrent_calls=$(clamp "${VANTA_MODULE_OPTION_CONCURRENT_CALLS:-$derived_concurrency}" 1 4)
max_memory_mb=$(clamp "${VANTA_MODULE_OPTION_MAX_MEMORY_MB:-$((cgroup_high_mb / concurrent_calls))}" 128 4096)
memory_mb=$(clamp "${VANTA_MODULE_OPTION_MEMORY_MB:-$((max_memory_mb * 2 / 3))}" 128 "$max_memory_mb")
max_timeout_ms=$(clamp "${VANTA_MODULE_OPTION_MAX_TIMEOUT_MS:-600000}" 1000 600000)
default_timeout_ms=$(clamp 60000 1000 "$max_timeout_ms")
calls_per_minute=$(clamp "${VANTA_MODULE_OPTION_CALLS_PER_MINUTE:-$((12 * concurrent_calls))}" 1 120)
cpu_quota=$(( (cores - 1) * 100 ))
if [ "$cpu_quota" -lt 100 ]; then cpu_quota=100; fi
tasks_max=$(( 64 * concurrent_calls + 32 ))
progress environment "$step" "$total" "Sizing limits for ${mem_total_mb} MB RAM and ${cores} cores: ${memory_mb}/${max_memory_mb} MB per call, ${concurrent_calls} concurrent"

{
	printf 'VANTA_PYTHON_CALLER_UID=%s\n' "$caller_uid"
	printf 'VANTA_PYTHON_CLIENT_GID=%s\n' "$caller_gid"
	printf 'VANTA_PYTHON_BUNDLE=%s\n' "$bundle"
	printf 'VANTA_PYTHON_DEFAULT_MEMORY_MB=%s\n' "$memory_mb"
	printf 'VANTA_PYTHON_MAX_MEMORY_MB=%s\n' "$max_memory_mb"
	printf 'VANTA_PYTHON_DEFAULT_TIMEOUT_MS=%s\n' "$default_timeout_ms"
	printf 'VANTA_PYTHON_MAX_TIMEOUT_MS=%s\n' "$max_timeout_ms"
	printf 'VANTA_PYTHON_CONCURRENT_CALLS=%s\n' "$concurrent_calls"
	printf 'VANTA_PYTHON_CALLS_PER_MINUTE=%s\n' "$calls_per_minute"
	if [ -n "${VANTA_ARTIFACT_ROOT:-}" ]; then
		printf 'VANTA_ARTIFACT_ROOT=%s\n' "$VANTA_ARTIFACT_ROOT"
		printf 'VANTA_ARTIFACT_PROTOCOL_VERSION=%s\n' "${VANTA_ARTIFACT_PROTOCOL_VERSION:-1}"
	fi
} > "$env_tmp"
chmod 0644 "$env_tmp"
mv -f "$env_tmp" /etc/vantamcpd/python-compute.env

install -d -m 0755 "$dropin_dir"
{
	printf '# Generated by the VantaMCPd python-compute installer from this node.\n[Service]\n'
	printf 'MemoryHigh=%sM\nMemoryMax=%sM\nCPUQuota=%s%%\nTasksMax=%s\n' "$cgroup_high_mb" "$cgroup_max_mb" "$cpu_quota" "$tasks_max"
	if [ -n "${VANTA_ARTIFACT_ROOT:-}" ]; then
		printf 'Group=%s\nReadWritePaths=%s\n' "$caller_group" "$VANTA_ARTIFACT_ROOT"
	fi
} > "$dropin_tmp"
chmod 0644 "$dropin_tmp"
mv -f "$dropin_tmp" "$dropin_dir/limits.conf"
install -d -m 0700 -o "$service_user" -g "$service_user" "$state_dir"

parent=$(dirname "$VANTA_MODULE_INSTALL_DIR")
mkdir -p "$parent"
rm -rf "$tmp"
mkdir -p "$tmp"
cp -a "$VANTA_MODULE_STAGE/." "$tmp/"

step=$((step + 1))
progress environment "$step" "$total" "Recording the package inventory"
python3 "$tmp/inventory.py" --bundle "$bundle" --write "$tmp/environment.json"
mkdir -p "$tmp/mplconfig"
if python3 -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('matplotlib') else 1)"; then
	MPLCONFIGDIR="$tmp/mplconfig" MPLBACKEND=Agg python3 -c "from matplotlib import font_manager; font_manager.FontManager()" >/dev/null
fi
chmod -R a+rX "$tmp"

step=$((step + 1))
progress verify "$step" "$total" "Checking the module self-tests"
python3 "$tmp/schemas.py"
python3 "$tmp/inventory.py" --self-test
python3 "$tmp/runner.py" --self-test
python3 "$tmp/sandbox.py"
python3 "$tmp/server.py" --self-test

step=$((step + 1))
progress verify "$step" "$total" "Starting a sandbox and confirming it is isolated"
runuser -u "$service_user" -- env PYTHONDONTWRITEBYTECODE=1 VANTA_PYTHON_INSTALL_DIR="$tmp" VANTA_PYTHON_STATE_DIR="$state_dir" \
	python3 "$tmp/sandbox.py" --smoke-test

rm -rf "$VANTA_MODULE_INSTALL_DIR"
mv "$tmp" "$VANTA_MODULE_INSTALL_DIR"
ln -sfn "$(basename "$VANTA_MODULE_INSTALL_DIR")" "$VANTA_MODULE_CURRENT_LINK"
trap - EXIT
rm -f "$env_tmp" 2>/dev/null || true
progress complete "$total" "$total" "Python Compute is ready"
