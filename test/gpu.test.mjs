import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { parseKeyValueLines } from "../dist/format.js";
import {
  buildGpuSetupScript,
  buildGpuTestScript,
  summarizeGpuInspection,
  validateContainerImage,
  GPU_APT_PIN,
  GPU_INSPECT_SCRIPT,
} from "../dist/gpu.js";

/** A promotable Debian 13 VM with a passed-through A4000 and no driver yet. */
const READY = [
  "arch|amd64",
  "os_id|debian",
  "os_version|13",
  "os_name|Debian GNU/Linux 13 (trixie)",
  "kernel|6.12.0-1-amd64",
  "virt|kvm",
  "free_root_mb|48000",
  "gpu_pci|0000:01:00.0 0x24b0",
  "gpu_count|1",
  "gpu_name|01:00.0 VGA compatible controller: NVIDIA Corporation GA104GL [RTX A4000]",
  "secure_boot|disabled",
  "headers_meta|yes",
  "headers_running|yes",
  "nvidia_module|0",
  "nouveau_module|1",
].join("\n");

function summarize(lines) {
  return summarizeGpuInspection(parseKeyValueLines(lines));
}

test("a passed-through GPU on a supported Debian is ready to promote", () => {
  const summary = summarize(READY);
  assert.equal(summary.state, "ready-to-promote");
  assert.deepEqual(summary.blockers, []);
  assert.equal(summary.system.cudaRepo, "debian13");
  assert.equal(summary.gpus.length, 1);
  assert.match(summary.next, /action="enable"/);
});

test("promotion blockers name the actual obstacle rather than failing late on the node", () => {
  const noGpu = summarize(READY.replace("gpu_count|1", "gpu_count|0").replace("gpu_pci|0000:01:00.0 0x24b0\n", ""));
  assert.equal(noGpu.state, "not-eligible");
  assert.match(noGpu.blockers.join(" "), /passed through/);

  const wrongOs = summarize(READY.replace("os_id|debian", "os_id|ubuntu").replace("os_version|13", "os_version|24.04"));
  assert.match(wrongOs.blockers.join(" "), /no CUDA repository for ubuntu 24\.04/);

  const tiny = summarize(READY.replace("free_root_mb|48000", "free_root_mb|2000"));
  assert.match(tiny.blockers.join(" "), /free on \//);

  const armhf = summarize(READY.replace("arch|amd64", "arch|armhf"));
  assert.match(armhf.blockers.join(" "), /amd64 and arm64/);
});

test("Secure Boot is a warning, because it breaks module loading rather than the build", () => {
  const summary = summarize(READY.replace("secure_boot|disabled", "secure_boot|enabled"));
  assert.equal(summary.state, "ready-to-promote");
  assert.match(summary.warnings.join(" "), /Secure Boot is enabled/);
  assert.match(summary.warnings.join(" "), /MOK/);
});

test("an installed driver whose module never loaded asks for a reboot, not another install", () => {
  const summary = summarize([READY, "pkg|nvidia-open", "pkg|nvidia-container-toolkit"].join("\n"));
  assert.equal(summary.state, "reboot-required");
  assert.match(summary.next, /Reboot with cluster_power/);
});

test("a fully promoted node reports enabled with its driver, CDI devices and receipt", () => {
  const summary = summarize(
    [
      READY.replace("nvidia_module|0", "nvidia_module|1"),
      "pkg|nvidia-open",
      "pkg|nvidia-container-toolkit",
      "pkg|podman",
      "driver_version|580.95.05",
      "cuda_version|13.4",
      "gpu|NVIDIA RTX A4000, 16376, Default, Enabled",
      "cdi_spec|/etc/cdi/nvidia.yaml",
      "cdi_device|nvidia.com/gpu=all",
      "cdi_device|nvidia.com/gpu=0",
      "podman|5.4.1",
      'receipt|{"schemaVersion":1,"driverPackage":"nvidia-open","rebootRequired":false}',
    ].join("\n"),
  );
  assert.equal(summary.state, "enabled");
  assert.equal(summary.driver.version, "580.95.05");
  assert.equal(summary.driver.moduleLoaded, true);
  assert.deepEqual(summary.container.cdiDevices, ["nvidia.com/gpu=all", "nvidia.com/gpu=0"]);
  assert.equal(summary.gpus[0].memoryMb, "16376");
  assert.equal(summary.gpus[0].computeMode, "Default");
  assert.equal(summary.receipt.driverPackage, "nvidia-open");
});

test("the probe reads the CUDA version from both nvidia-smi header spellings", () => {
  // Driver 615 prints "CUDA UMD Version"; earlier drivers print "CUDA Version". Missing it silently
  // breaks any module that declares minRuntimeVersion.
  assert.match(GPU_INSPECT_SCRIPT, /CUDA \\\(UMD \\\)\\\?Version/);
});

test("the promotion script installs a driver and the container toolkit without the CUDA toolkit", () => {
  const script = buildGpuSetupScript({ driver: "open", containerRuntime: "podman" });
  assert.match(script, /install -- nvidia-open/);
  assert.match(script, /apt-mark manual nvidia-open/);
  assert.match(script, /install -- nvidia-container-toolkit/);
  assert.match(script, /nvidia-ctk cdi generate --output='\/etc\/cdi\/nvidia\.yaml'/);
  assert.doesNotMatch(script, /install -- cuda-toolkit/);
  // The node must not be left in a state the operator cannot see.
  assert.match(script, /INCOMPLETE: reboot the node/);
  assert.match(script, /\/var\/lib\/vantamcpd\/gpu\.json/);
  // Unattended apt, matching every other remote package operation.
  assert.match(script, /DEBIAN_FRONTEND=noninteractive/);
  assert.match(script, /DPkg::Lock::Timeout=300/);
});

test("the promotion script is re-runnable: every step checks before it changes the node", () => {
  const script = buildGpuSetupScript({ driver: "open", containerRuntime: "podman" });
  assert.match(script, /dpkg -s cuda-keyring >\/dev\/null 2>&1/);
  assert.match(script, /dpkg -s nvidia-container-toolkit >\/dev\/null 2>&1/);
  assert.match(script, /grep -qE '\^Components:\.\*\[\[:space:\]\]contrib/);
  assert.match(script, /cp -a "\$sources"/);
});

test("the CUDA repository is pinned to the driver stack, so it cannot replace Debian's own packages", () => {
  // Matching the release label rather than the host survives a mirror change; verified on a live node.
  assert.match(GPU_APT_PIN, /Package: \*\nPin: release l=NVIDIA CUDA\nPin-Priority: 100/);
  assert.match(GPU_APT_PIN, /Pin: release l=NVIDIA CUDA\nPin-Priority: 600/);
  for (const family of ["*nvidia*", "cuda-*", "libcuda*", "libnccl*", "libnvcuvid*", "libnvoptix*", "libxnvctrl*"]) {
    assert.ok(GPU_APT_PIN.includes(family), `pin must keep ${family} on NVIDIA's build`);
  }

  const script = buildGpuSetupScript({ driver: "open", containerRuntime: "podman" });
  assert.match(script, /\/etc\/apt\/preferences\.d\/vantamcpd-cuda/);
  assert.ok(script.includes(Buffer.from(GPU_APT_PIN, "utf8").toString("base64")), "the pin is staged verbatim");
  // Re-running must not rewrite an unchanged pin.
  assert.ok(script.includes(createHash("sha256").update(GPU_APT_PIN).digest("hex")));
  assert.match(script, /pin already current/);
  // The pin has to exist before the repository it constrains is added.
  assert.ok(script.indexOf("apt pinning") < script.indexOf("cuda repository"));
});

test("each phase reports progress, so a multi-minute step is not a blank dashboard", () => {
  const script = buildGpuSetupScript({ driver: "open", containerRuntime: "podman" });
  // The job runner only picks up phase and progress from lines with this prefix.
  assert.match(script, /printf 'VANTA_PROGRESS \{"phase":"%s","current":%s,"total":%s,"unit":"steps","message":"%s"\}/);
  assert.match(script, /total_phases=11/);
  assert.equal(script.split("\n").filter((line) => line.startsWith("phase ")).length, 11);
  // The two longest steps say what they are doing rather than going quiet.
  assert.match(script, /progress "installing nvidia-open and building the kernel module - several minutes"/);
  assert.match(script, /progress "installing podman and the NVIDIA Container Toolkit"/);

  const driverOnly = buildGpuSetupScript({ driver: "open", containerRuntime: "none" });
  assert.match(driverOnly, /total_phases=9/);
  assert.equal(driverOnly.split("\n").filter((line) => line.startsWith("phase ")).length, 9);
});

test("driver flavour and container runtime change what the promotion installs", () => {  const proprietary = buildGpuSetupScript({ driver: "proprietary", containerRuntime: "docker" });
  assert.match(proprietary, /install -- cuda-drivers/);
  assert.match(proprietary, /nvidia-ctk runtime configure --runtime=docker/);
  assert.doesNotMatch(proprietary, /install -- podman/);

  const driverOnly = buildGpuSetupScript({ driver: "open", containerRuntime: "none" });
  assert.doesNotMatch(driverOnly, /nvidia-container-toolkit/);
  assert.doesNotMatch(driverOnly, /nvidia-ctk/);
});

test("the smoke test injects the host driver into a plain base image", () => {
  const podman = buildGpuTestScript("podman", "docker.io/library/debian:13");
  assert.match(podman, /podman run --rm --device nvidia\.com\/gpu=all 'docker\.io\/library\/debian:13' nvidia-smi/);
  assert.match(podman, /--query-compute-apps/);
  assert.match(buildGpuTestScript("docker", "debian:13"), /docker run --rm --gpus all 'debian:13' nvidia-smi/);
});

test("container image references are validated before they reach the shell", () => {
  assert.equal(validateContainerImage("docker.io/library/debian:13"), "docker.io/library/debian:13");
  assert.equal(validateContainerImage(`debian@sha256:${"a".repeat(64)}`), `debian@sha256:${"a".repeat(64)}`);
  for (const bad of ["debian:13; rm -rf /", "debian 13", "$(id)", "debian:13\nnvidia-smi", "-debian"]) {
    assert.throws(() => validateContainerImage(bad), /Invalid container image reference/);
  }
});
