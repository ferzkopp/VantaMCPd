#!/usr/bin/env bash
set -euo pipefail

: "${VANTA_MODULE_STAGE:?}"
: "${VANTA_MODULE_INSTALL_DIR:?}"
: "${VANTA_MODULE_CURRENT_LINK:?}"
: "${VANTA_MODULE_RUN_AS:?}"

state=/var/lib/vantamcpd-document-ocr
image=localhost/vantamcpd-document-ocr:0.1.0
uid=$(id -u "$VANTA_MODULE_RUN_AS")
home=$(getent passwd "$VANTA_MODULE_RUN_AS" | cut -d: -f6)
for file in Containerfile module.json server.py worker.py artifact_protocol.py; do
    test -f "$VANTA_MODULE_STAGE/$file"
done
for command in podman python3 runuser nvidia-smi; do
    command -v "$command" >/dev/null
done

progress() {
    printf 'VANTA_PROGRESS {"phase":"%s","current":%d,"total":4,"unit":"steps","message":"%s"}\n' "$1" "$2" "$3"
}

as_caller() {
    ( cd "$home" && runuser -u "$VANTA_MODULE_RUN_AS" -- env HOME="$home" XDG_RUNTIME_DIR="/run/user/$uid" "$@" )
}

progress environment 0 'Preparing rootless Podman and model cache'
install -d -m 0700 -o "$VANTA_MODULE_RUN_AS" -g "$(id -gn "$VANTA_MODULE_RUN_AS")" "$state" "$state/models" "$state/calls"
as_caller podman --cgroup-manager=cgroupfs info >/dev/null

parent=$(dirname "$VANTA_MODULE_INSTALL_DIR")
tmp="$VANTA_MODULE_INSTALL_DIR.tmp.$$"
mkdir -p "$parent" "$tmp"
trap 'rm -rf -- "$tmp"' EXIT
cp -a "$VANTA_MODULE_STAGE/." "$tmp/"
chmod -R a+rX "$tmp"

progress image 1 'Building the pinned CUDA OCR image'
as_caller podman --cgroup-manager=cgroupfs build --pull=missing -t "$image" -f "$tmp/Containerfile" "$tmp"
progress models 2 'Loading OCR models through the GPU CDI device'
as_caller podman --cgroup-manager=cgroupfs run --rm --network=slirp4netns --device nvidia.com/gpu=all \
    -e PADDLE_PDX_CACHE_HOME=/models -e PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    -v "$state/models:/models:rw" "$image" --warmup
# Repeat under call conditions so a missed cache fails here instead of on the first call.
as_caller podman --cgroup-manager=cgroupfs run --rm --network=none --tmpfs /tmp:rw,size=256m \
    --device nvidia.com/gpu=all -e HOME=/tmp -e PADDLE_PDX_CACHE_HOME=/models -e PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    -v "$state/models:/models:ro" "$image" --warmup
progress verify 3 'Verifying MCP entrypoint and worker'
PYTHONPATH="$tmp" PYTHONDONTWRITEBYTECODE=1 python3 -B "$tmp/server.py" --self-test
python3 -B "$tmp/worker.py" --self-test

rm -rf -- "$VANTA_MODULE_INSTALL_DIR"
mv -- "$tmp" "$VANTA_MODULE_INSTALL_DIR"
ln -sfn "$(basename "$VANTA_MODULE_INSTALL_DIR")" "$VANTA_MODULE_CURRENT_LINK"
trap - EXIT
progress complete 4 'Document OCR is ready'