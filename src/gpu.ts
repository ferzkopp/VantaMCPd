/**
 * Promotion of an already-enrolled node to a CUDA node: inspection, the durable setup script, and the
 * container smoke test. Enrollment itself stays identical for every node - nothing here runs before a
 * node is reachable, and a node that is never promoted is unaffected.
 */
import { createHash } from "node:crypto";
import { aptGet, aptUpdate } from "./apt.js";
import { q } from "./security.js";

export type GpuDriverFlavour = "open" | "proprietary";
export type GpuContainerRuntime = "podman" | "docker" | "none";

/** Root-owned marker describing what the promotion actually installed. */
export const GPU_RECEIPT_PATH = "/var/lib/vantamcpd/gpu.json";
export const GPU_SETUP_SCRIPT_PATH = "/var/lib/vantamcpd/gpu-setup.sh";
export const GPU_CDI_SPEC_PATH = "/etc/cdi/nvidia.yaml";
export const GPU_APT_PIN_PATH = "/etc/apt/preferences.d/vantamcpd-cuda";
export const GPU_JOB_KIND = "gpu-setup";

/**
 * NVIDIA publishes a pin for Ubuntu only, and it claims the whole repository (`l=NVIDIA CUDA` at 600),
 * which also hands it packages Debian maintains - `dkms` and `dh-dkms` most visibly. A managed node
 * keeps those on the distribution version: the driver only requires `dkms (>= 3.1.8)`, which Debian
 * satisfies. So demote the repository and raise back the families the driver stack owns and versions
 * together. Matching on the release label rather than the host survives a mirror or CDN change.
 */
export const GPU_APT_PIN = `# Managed by VantaMCPd. The NVIDIA CUDA repository supplies the driver stack only;
# anything Debian also ships - dkms, dh-dkms, toolchains - stays on the distribution version.
Package: *
Pin: release l=NVIDIA CUDA
Pin-Priority: 100

Package: *nvidia* cuda-* libcuda* libcudnn* libnccl* libnvcuvid* libnvoptix* libxnvctrl* nsight-* gds-tools*
Pin: release l=NVIDIA CUDA
Pin-Priority: 600
`;

/** Debian releases with a CUDA repository of their own. */
const SUPPORTED_DISTRIBUTIONS = new Map([
  ["debian:12", "debian12"],
  ["debian:13", "debian13"],
]);
const SUPPORTED_ARCHITECTURES = new Map([
  ["amd64", "x86_64"],
  ["arm64", "sbsa"],
]);
/** Driver, container images and the DKMS build all land on the root filesystem. */
const MIN_FREE_ROOT_MB = 8_000;

const DRIVER_PACKAGES: Record<GpuDriverFlavour, string> = {
  open: "nvidia-open",
  proprietary: "cuda-drivers",
};

/**
 * Read-only probe. Reports both promotion readiness and the current CUDA state, so one action answers
 * "can this node be promoted?" before and "did the promotion work?" after.
 */
export const GPU_INSPECT_SCRIPT = [
  `LC_ALL=C`,
  `echo "arch|$(dpkg --print-architecture 2>/dev/null)"`,
  `if [ -r /etc/os-release ]; then . /etc/os-release; echo "os_id|$ID"; echo "os_version|$VERSION_ID"; echo "os_name|$PRETTY_NAME"; fi`,
  `echo "kernel|$(uname -r)"`,
  `echo "virt|$(systemd-detect-virt 2>/dev/null || echo none)"`,
  `echo "free_root_mb|$(df -Pm / | awk 'NR==2{print $4}')"`,
  // sysfs rather than lspci: pciutils is not part of the node baseline.
  `gpu_count=0`,
  `for d in /sys/bus/pci/devices/*; do`,
  `  [ -r "$d/vendor" ] || continue`,
  `  [ "$(cat "$d/vendor")" = "0x10de" ] || continue`,
  `  case "$(cat "$d/class")" in 0x03*) ;; *) continue ;; esac`,
  `  gpu_count=$((gpu_count + 1))`,
  `  echo "gpu_pci|$(basename "$d") $(cat "$d/device")"`,
  `done`,
  `echo "gpu_count|$gpu_count"`,
  `if command -v lspci >/dev/null 2>&1; then lspci -nn 2>/dev/null | grep -iE 'vga|3d controller|display' | grep -i nvidia | sed 's/^/gpu_name|/'; fi`,
  `sb=unknown`,
  `if [ ! -d /sys/firmware/efi ]; then sb=disabled`,
  `elif command -v mokutil >/dev/null 2>&1; then`,
  `  if mokutil --sb-state 2>/dev/null | grep -qi enabled; then sb=enabled; else sb=disabled; fi`,
  `else`,
  `  f=$(ls /sys/firmware/efi/efivars/SecureBoot-* 2>/dev/null | head -n1)`,
  `  if [ -n "$f" ]; then`,
  `    v=$(od -An -t u1 -j 4 -N 1 "$f" 2>/dev/null | tr -d ' ')`,
  `    if [ "$v" = "1" ]; then sb=enabled; elif [ "$v" = "0" ]; then sb=disabled; fi`,
  `  fi`,
  `fi`,
  `echo "secure_boot|$sb"`,
  `for p in dkms cuda-keyring nvidia-open cuda-drivers nvidia-container-toolkit podman docker.io docker-ce; do`,
  `  if dpkg -s "$p" >/dev/null 2>&1; then echo "pkg|$p"; fi`,
  `done`,
  `if dpkg -s "linux-headers-$(dpkg --print-architecture)" >/dev/null 2>&1; then echo "headers_meta|yes"; else echo "headers_meta|no"; fi`,
  `if dpkg -s "linux-headers-$(uname -r)" >/dev/null 2>&1; then echo "headers_running|yes"; else echo "headers_running|no"; fi`,
  `echo "nvidia_module|$(lsmod 2>/dev/null | grep -c '^nvidia ')"`,
  `echo "nouveau_module|$(lsmod 2>/dev/null | grep -c '^nouveau ')"`,
  `if command -v nvidia-smi >/dev/null 2>&1; then`,
  `  echo "driver_version|$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1)"`,
  `  echo "cuda_version|$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA \\(UMD \\)\\?Version: *\\([0-9.]*\\).*/\\2/p' | head -n1)"`,
  `  nvidia-smi --query-gpu=name,memory.total,compute_mode,persistence_mode --format=csv,noheader 2>/dev/null | sed 's/^/gpu|/'`,
  `fi`,
  `if command -v dkms >/dev/null 2>&1; then dkms status 2>/dev/null | grep -i nvidia | sed 's/^/dkms|/'; fi`,
  `if [ -f ${q(GPU_CDI_SPEC_PATH)} ]; then echo "cdi_spec|${GPU_CDI_SPEC_PATH}"; fi`,
  `if command -v nvidia-ctk >/dev/null 2>&1; then nvidia-ctk cdi list 2>/dev/null | grep -oE 'nvidia\\.com/gpu=[A-Za-z0-9._-]+' | sed 's/^/cdi_device|/'; fi`,
  `if command -v podman >/dev/null 2>&1; then echo "podman|$(podman --version 2>/dev/null | awk '{print $3}')"; fi`,
  `if command -v docker >/dev/null 2>&1; then echo "docker|$(docker --version 2>/dev/null | awk '{print $3}' | tr -d ,)"; fi`,
  `if [ -f ${q(GPU_RECEIPT_PATH)} ]; then echo "receipt|$(tr -d '\\n' < ${q(GPU_RECEIPT_PATH)})"; fi`,
  `if [ -f /var/run/reboot-required ]; then echo "reboot_pending|yes"; fi`,
  // Trailing probes are allowed to fail; the parsed keys, not the exit code, carry the answer.
  `exit 0`,
].join("\n");

export interface GpuInspection {
  state: "not-eligible" | "ready-to-promote" | "reboot-required" | "enabled";
  blockers: string[];
  warnings: string[];
  next: string;
  system: {
    os?: string;
    arch?: string;
    kernel?: string;
    virtualization?: string;
    secureBoot?: string;
    freeRootMb?: number;
    cudaRepo?: string;
    kernelHeaders: boolean;
  };
  gpus: { pci?: string; name?: string; memoryMb?: string; computeMode?: string; persistenceMode?: string }[];
  driver: {
    packages: string[];
    version?: string;
    cudaVersion?: string;
    moduleLoaded: boolean;
    nouveauLoaded: boolean;
    dkms: string[];
  };
  container: { runtimes: string[]; toolkitInstalled: boolean; cdiSpec?: string; cdiDevices: string[] };
  receipt?: unknown;
}

function list(value: string | string[] | undefined): string[] {
  if (value === undefined) return [];
  return Array.isArray(value) ? value : [value];
}

/** Turn the probe output into a decision: what blocks promotion, and what the next step is. */
export function summarizeGpuInspection(kv: Record<string, string | string[]>): GpuInspection {
  const single = (key: string): string | undefined => {
    const value = kv[key];
    const first = Array.isArray(value) ? value[0] : value;
    return first === undefined || first === "" ? undefined : first;
  };

  const arch = single("arch");
  const osId = single("os_id");
  const osVersion = single("os_version");
  const freeRootMb = single("free_root_mb") ? Number(single("free_root_mb")) : undefined;
  const secureBoot = single("secure_boot");
  const gpuCount = Number(single("gpu_count") ?? 0);
  const packages = list(kv.pkg);
  const cudaRepo = SUPPORTED_DISTRIBUTIONS.get(`${osId}:${osVersion}`);

  const blockers: string[] = [];
  const warnings: string[] = [];
  if (gpuCount === 0) {
    blockers.push("no NVIDIA display device is visible on the PCI bus (on a VM, check that the card is passed through)");
  }
  if (arch !== undefined && !SUPPORTED_ARCHITECTURES.has(arch)) {
    blockers.push(`CUDA promotion supports amd64 and arm64; this node reports ${arch}`);
  }
  if (cudaRepo === undefined) {
    blockers.push(
      `no CUDA repository for ${osId ?? "unknown"} ${osVersion ?? ""}`.trim() +
        "; supported: " + [...SUPPORTED_DISTRIBUTIONS.keys()].join(", "),
    );
  }
  if (freeRootMb !== undefined && freeRootMb < MIN_FREE_ROOT_MB) {
    blockers.push(`needs about ${MIN_FREE_ROOT_MB} MB free on /, found ${freeRootMb} MB`);
  }
  if (secureBoot === "enabled") {
    warnings.push(
      "Secure Boot is enabled: the DKMS-built module is unsigned, so it will compile but never load unless a MOK is enrolled",
    );
  }
  if (single("headers_meta") !== "yes") warnings.push("kernel headers are missing; promotion installs them");

  const moduleLoaded = Number(single("nvidia_module") ?? 0) > 0 && single("driver_version") !== undefined;
  const driverInstalled = packages.some((name) => name === "nvidia-open" || name === "cuda-drivers");
  const toolkitInstalled = packages.includes("nvidia-container-toolkit");
  const cdiSpec = single("cdi_spec");

  let receipt: unknown;
  const receiptText = single("receipt");
  if (receiptText !== undefined) {
    try {
      receipt = JSON.parse(receiptText);
    } catch {
      warnings.push("the GPU receipt on the node is not valid JSON; re-run the promotion");
    }
  }

  let state: GpuInspection["state"];
  let next: string;
  if (moduleLoaded && toolkitInstalled && cdiSpec !== undefined) {
    state = "enabled";
    next = 'The node is CUDA-ready. Verify end to end with action="test".';
  } else if (driverInstalled && !moduleLoaded) {
    state = "reboot-required";
    next =
      "The driver is installed but its kernel module is not loaded. Reboot with cluster_power, then run " +
      'cluster_gpu action="enable" again to finish device injection.';
  } else if (blockers.length > 0) {
    state = "not-eligible";
    next = "Resolve the blockers before promoting this node.";
  } else {
    state = "ready-to-promote";
    next = 'Promote with cluster_gpu action="enable", confirm: true.';
  }

  return {
    state,
    blockers,
    warnings,
    next,
    system: {
      os: single("os_name"),
      arch,
      kernel: single("kernel"),
      virtualization: single("virt"),
      secureBoot,
      freeRootMb,
      cudaRepo,
      kernelHeaders: single("headers_meta") === "yes" || single("headers_running") === "yes",
    },
    gpus: gpuDevices(list(kv.gpu_pci), list(kv.gpu_name), list(kv.gpu)),
    driver: {
      packages,
      version: single("driver_version"),
      cudaVersion: single("cuda_version"),
      moduleLoaded,
      nouveauLoaded: Number(single("nouveau_module") ?? 0) > 0,
      dkms: list(kv.dkms),
    },
    container: {
      runtimes: ["podman", "docker"].filter((runtime) => single(runtime) !== undefined),
      toolkitInstalled,
      cdiSpec,
      cdiDevices: list(kv.cdi_device),
    },
    ...(receipt === undefined ? {} : { receipt }),
  };
}

/** `nvidia-smi` rows are authoritative when the driver runs; otherwise fall back to the PCI listing. */
function gpuDevices(pci: string[], names: string[], smi: string[]): GpuInspection["gpus"] {
  if (smi.length > 0) {
    return smi.map((row, index) => {
      const [name, memoryMb, computeMode, persistenceMode] = row.split(/,\s*/);
      return { pci: pci[index], name, memoryMb, computeMode, persistenceMode };
    });
  }
  return pci.map((entry, index) => ({ pci: entry, name: names[index] }));
}

export interface GpuSetupOptions {
  driver: GpuDriverFlavour;
  containerRuntime: GpuContainerRuntime;
}

/**
 * The promotion itself. Runs as root under the durable job runner, is safe to re-run, and stops short
 * of rebooting: when the module cannot be loaded in place it reports that a reboot is owed instead.
 */
export function buildGpuSetupScript({ driver, containerRuntime }: GpuSetupOptions): string {
  const driverPackage = DRIVER_PACKAGES[driver];
  // Named here so the script can report "step n of m"; the phase() calls below use the same names.
  const phases = [
    "preflight",
    "apt components",
    "apt pinning",
    "prerequisites",
    "cuda repository",
    "driver",
    "driver load",
    ...(containerRuntime === "none" ? [] : ["container runtime", "device injection"]),
    "receipt",
    "summary",
  ];
  const lines = [
    `#!/usr/bin/env bash`,
    `set -eo pipefail`,
    `export DEBIAN_FRONTEND=noninteractive`,
    `total_phases=${phases.length}`,
    `phase_index=0`,
    `current_phase=starting`,
    // The job runner turns these lines into job phase and progress; everything else is just log.
    `progress() { printf 'VANTA_PROGRESS {"phase":"%s","current":%s,"total":%s,"unit":"steps","message":"%s"}\\n' "$current_phase" "$phase_index" "$total_phases" "$1"; }`,
    `phase() { current_phase="$1"; phase_index=$((phase_index + 1)); echo; echo "== $1 =="; progress "\${2:-$1}"; }`,
    `fail() { echo "ERROR: $*" >&2; exit 1; }`,
    ``,
    `phase preflight`,
    `[ "$(id -u)" = "0" ] || fail "the promotion script must run as root"`,
    `. /etc/os-release`,
    `arch=$(dpkg --print-architecture)`,
    `case "$ID:$VERSION_ID" in`,
    ...[...SUPPORTED_DISTRIBUTIONS.entries()].map(([key, repo]) => `  ${key}) cuda_repo=${repo} ;;`),
    `  *) fail "no CUDA repository for $ID $VERSION_ID" ;;`,
    `esac`,
    `case "$arch" in`,
    ...[...SUPPORTED_ARCHITECTURES.entries()].map(([key, repoArch]) => `  ${key}) repo_arch=${repoArch} ;;`),
    `  *) fail "CUDA promotion supports amd64 and arm64, not $arch" ;;`,
    `esac`,
    `gpu_count=0`,
    `for d in /sys/bus/pci/devices/*; do`,
    `  [ -r "$d/vendor" ] || continue`,
    `  [ "$(cat "$d/vendor")" = "0x10de" ] || continue`,
    `  case "$(cat "$d/class")" in 0x03*) gpu_count=$((gpu_count + 1)) ;; esac`,
    `done`,
    `[ "$gpu_count" -gt 0 ] || fail "no NVIDIA display device on the PCI bus"`,
    `echo "node: $PRETTY_NAME, $arch, kernel $(uname -r), $gpu_count NVIDIA device(s)"`,
    `echo "repository: $cuda_repo/$repo_arch, driver package: ${driverPackage}"`,
    ``,
    `phase "apt components"`,
    // NVIDIA's Debian packaging pulls support packages that live in contrib.
    `sources=/etc/apt/sources.list.d/debian.sources`,
    `if [ -f "$sources" ]; then`,
    `  if ! grep -qE '^Components:.*[[:space:]]contrib([[:space:]]|$)' "$sources"; then`,
    `    cp -a "$sources" "$sources.vantamcpd-$(date +%Y%m%d%H%M%S).bak"`,
    `    sed -i -E '/^Components:/ s/$/ contrib/' "$sources"`,
    `    echo "enabled contrib in $sources"`,
    `  else echo "contrib already enabled"; fi`,
    `elif [ -f /etc/apt/sources.list ]; then`,
    `  if ! grep -qE '^deb .*[[:space:]]contrib([[:space:]]|$)' /etc/apt/sources.list; then`,
    `    cp -a /etc/apt/sources.list "/etc/apt/sources.list.vantamcpd-$(date +%Y%m%d%H%M%S).bak"`,
    `    sed -i -E '/^deb .*(debian\\.org|debian\\.map\\.fastlydns\\.net)/ s/[[:space:]]main([[:space:]]|$)/ main contrib\\1/' /etc/apt/sources.list`,
    `    echo "enabled contrib in /etc/apt/sources.list"`,
    `  else echo "contrib already enabled"; fi`,
    `fi`,
    ``,
    `phase "apt pinning"`,
    `pin=${q(GPU_APT_PIN_PATH)}`,
    // Compared by digest: $(cat) strips the trailing newline, so a literal comparison never matches.
    `if [ -f "$pin" ] && [ "$(sha256sum "$pin" | awk '{print $1}')" = ${q(createHash("sha256").update(GPU_APT_PIN).digest("hex"))} ]; then`,
    `  echo "pin already current"`,
    `else`,
    `  printf '%s' ${q(Buffer.from(GPU_APT_PIN, "utf8").toString("base64"))} | base64 -d > "$pin"`,
    `  chmod 0644 "$pin"`,
    `  echo "wrote $pin: NVIDIA packages from NVIDIA, everything else from Debian"`,
    `fi`,
    ``,
    `phase prerequisites`,
    `progress "installing kernel headers and the DKMS toolchain"`,
    aptUpdate(),
    aptGet("install", { packages: ['"linux-headers-$arch"', "dkms", "curl", "ca-certificates", "gnupg"] }),
    // A backported or vendor kernel may have no meta-package; its own headers package is what DKMS needs.
    `${aptGet("install", { packages: ['"linux-headers-$(uname -r)"'] })} || echo "note: no headers package for the running kernel"`,
    ``,
    `phase "cuda repository"`,
    `if dpkg -s cuda-keyring >/dev/null 2>&1; then`,
    `  echo "cuda-keyring already installed"`,
    `else`,
    `  tmp=$(mktemp -d)`,
    `  trap 'rm -rf "$tmp"' EXIT`,
    `  curl -fsSL --retry 3 -o "$tmp/cuda-keyring.deb" \\`,
    `    "https://developer.download.nvidia.com/compute/cuda/repos/$cuda_repo/$repo_arch/cuda-keyring_1.1-1_all.deb"`,
    `  dpkg -i "$tmp/cuda-keyring.deb"`,
    `fi`,
    aptUpdate(),
    ``,
    `phase driver`,
    `progress "installing ${driverPackage} and building the kernel module - several minutes"`,
    aptGet("install", { packages: [driverPackage] }),
    // Toolkit packages no longer depend on the driver, so an unrelated autoremove can otherwise take it.
    `apt-mark manual ${driverPackage} >/dev/null`,
    ``,
    `phase "driver load"`,
    `reboot_required=0`,
    `if ! lsmod | grep -q '^nvidia '; then`,
    `  modprobe -r nouveau 2>/dev/null || true`,
    `  modprobe nvidia 2>/dev/null || reboot_required=1`,
    `fi`,
    `if nvidia-smi -L; then`,
    `  driver_version=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1 | tr -cd '[:alnum:].-')`,
    // On-demand modules start a process per call; persistence keeps the driver initialized between them.
    `  systemctl enable --now nvidia-persistenced 2>/dev/null || echo "note: nvidia-persistenced is unavailable"`,
    `else`,
    `  reboot_required=1`,
    `  driver_version=""`,
    `  echo "the kernel module is not usable yet; a reboot is required"`,
    `fi`,
  ];

  if (containerRuntime !== "none") {
    lines.push(
      ``,
      `phase "container runtime"`,
      `progress "installing ${containerRuntime === "podman" ? "podman and " : ""}the NVIDIA Container Toolkit"`,
      containerRuntime === "podman"
        ? `dpkg -s podman >/dev/null 2>&1 || ${aptGet("install", { packages: ["podman"] })}`
        : `command -v docker >/dev/null 2>&1 || fail "docker is not installed; install it first or promote with containerRuntime=\\"podman\\""`,
      `keyring=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg`,
      `list=/etc/apt/sources.list.d/nvidia-container-toolkit.list`,
      `if [ ! -f "$keyring" ]; then`,
      `  curl -fsSL --retry 3 https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o "$keyring"`,
      `fi`,
      `if [ ! -f "$list" ]; then`,
      `  curl -fsSL --retry 3 https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list |`,
      `    sed "s#deb https://#deb [signed-by=$keyring] https://#g" > "$list"`,
      `  ${aptUpdate()}`,
      `fi`,
      `dpkg -s nvidia-container-toolkit >/dev/null 2>&1 || ${aptGet("install", { packages: ["nvidia-container-toolkit"] })}`,
      ``,
      `phase "device injection"`,
      `if [ "$reboot_required" = "0" ]; then`,
      `  install -d -m 0755 /etc/cdi`,
      // The spec pins driver library paths, so it is regenerated on every promotion run.
      `  nvidia-ctk cdi generate --output=${q(GPU_CDI_SPEC_PATH)}`,
      `  nvidia-ctk cdi list || true`,
      ...(containerRuntime === "docker"
        ? [`  nvidia-ctk runtime configure --runtime=docker`, `  systemctl restart docker`]
        : []),
      `else`,
      `  echo "skipping CDI generation until the driver is loaded"`,
      `fi`,
    );
  }

  lines.push(
    ``,
    `phase receipt`,
    `install -d -m 0755 /var/lib/vantamcpd`,
    `printf '{"schemaVersion":1,"driverPackage":"%s","driverVersion":"%s","containerRuntime":"%s","cudaRepo":"%s","cdiSpec":"%s","rebootRequired":%s,"promotedAt":"%s"}\\n' \\`,
    `  ${q(driverPackage)} "$driver_version" ${q(containerRuntime)} "$cuda_repo/$repo_arch" ${q(containerRuntime === "none" ? "" : GPU_CDI_SPEC_PATH)} \\`,
    `  "$([ "$reboot_required" = "1" ] && echo true || echo false)" "$(date -Is)" > ${q(GPU_RECEIPT_PATH)}`,
    `chmod 0644 ${q(GPU_RECEIPT_PATH)}`,
    `cat ${q(GPU_RECEIPT_PATH)}`,
    ``,
    `phase summary`,
    `if [ "$reboot_required" = "1" ]; then`,
    `  echo "INCOMPLETE: reboot the node, then run the promotion again to load the driver and generate the CDI spec."`,
    `else`,
    `  echo "COMPLETE: driver $driver_version is loaded and the node can run CUDA containers."`,
    `fi`,
    ``,
  );
  return lines.join("\n");
}

const IMAGE_RE = /^[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?(?:@sha256:[a-f0-9]{64})?$/;

export function validateContainerImage(image: string): string {
  if (!IMAGE_RE.test(image)) throw new Error(`Invalid container image reference: ${JSON.stringify(image)}`);
  return image;
}

/**
 * End-to-end check. The container toolkit injects the host driver and `nvidia-smi` itself, so a plain
 * base image proves passthrough, driver, CDI and runtime without pulling a multi-gigabyte CUDA image.
 */
export function buildGpuTestScript(runtime: Exclude<GpuContainerRuntime, "none">, image: string): string {
  const reference = q(validateContainerImage(image));
  const run =
    runtime === "podman"
      ? `podman run --rm --device nvidia.com/gpu=all ${reference} nvidia-smi`
      : `docker run --rm --gpus all ${reference} nvidia-smi`;
  return [
    `set -e`,
    `echo "== host =="`,
    `nvidia-smi -L`,
    `echo`,
    `echo "== ${runtime} container =="`,
    run,
    `echo`,
    `echo "== resident compute processes =="`,
    `nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory --format=csv || true`,
  ].join("\n");
}
