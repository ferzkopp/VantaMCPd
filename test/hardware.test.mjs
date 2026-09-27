import assert from "node:assert/strict";
import test from "node:test";

import { nodeCapability, parseHardware } from "../dist/hardware.js";

test("parses discovered GPU capabilities into the hardware inventory", () => {
  const hardware = parseHardware([
    "cpu_cores|24",
    "arch|x86_64",
    "accelerator|kind=gpu vendor=nvidia model=\"NVIDIA RTX 5090\" memory_mb=32768 runtime=cuda runtime_version=12.8",
    "accelerator|kind=gpu vendor=amd model=\"AMD Radeon PRO W7900\" memory_mb=49136 runtime=rocm runtime_version=6.3",
    "discovered_at|2026-09-13T20:00:00Z",
  ].join("\n"));

  assert.deepEqual(hardware.accelerators, [
    {
      kind: "gpu",
      vendor: "nvidia",
      model: "NVIDIA RTX 5090",
      memoryMb: 32768,
      runtime: "cuda",
      runtimeVersion: "12.8",
    },
    {
      kind: "gpu",
      vendor: "amd",
      model: "AMD Radeon PRO W7900",
      memoryMb: 49136,
      runtime: "rocm",
      runtimeVersion: "6.3",
    },
  ]);
});

test("records an empty accelerator inventory when no GPU tool reports a device", () => {
  const hardware = parseHardware("cpu_cores|2\narch|armv7l\ndiscovered_at|2026-09-13T20:00:00Z\n");

  assert.deepEqual(hardware.accelerators, []);
});

test("ranks node capacity by its weakest axis rather than by core count alone", () => {
  const rank = (hardware, storage) => nodeCapability({ name: "n", hardware, ...(storage ? { storage } : {}) });

  // Equal core counts, very different machines: the architecture weight has to separate them.
  const board = rank({ cpu: { packageArch: "armhf", cores: 4, maxMhz: 960 }, memory: { totalMb: 1000 } });
  const server = rank({ cpu: { packageArch: "amd64", cores: 4 }, memory: { totalMb: 3921 } });
  assert.equal(board.tier, "low");
  assert.equal(server.tier, "medium");

  // Plenty of fast cores cannot rescue a node that has no memory to use them with.
  assert.equal(rank({ cpu: { packageArch: "amd64", cores: 16, maxMhz: 3600 }, memory: { totalMb: 1024 } }).tier, "low");
  assert.equal(rank({ cpu: { packageArch: "amd64", cores: 8, maxMhz: 3600 }, memory: { totalMb: 32768 } }).tier, "high");

  // An undiscovered clock must not demote a node, and an unprobed node is reported as unknown.
  assert.equal(rank({ cpu: { packageArch: "amd64", cores: 4 }, memory: { totalMb: 8192 } }).tier, "medium");
  assert.equal(rank({}).tier, "unknown");
  assert.equal(rank({}).summary, "hardware inventory not discovered");

  const equipped = rank(
    { cpu: { packageArch: "amd64", cores: 8, maxMhz: 3600 }, memory: { totalMb: 32768 }, accelerators: [{ kind: "gpu", model: "RTX 4070" }] },
    { mountpoint: "/mnt/ssd" },
  );
  assert.equal(equipped.accelerator, "RTX 4070");
  assert.equal(equipped.storage, true);
  // The overlays carry these, so repeating them in the summary would double them in the tooltip.
  assert.equal(equipped.summary, "8 amd64 cores, 3600 MHz, 32768 MB RAM");
  assert.equal(board.storage, false);
});

test("records an fstab network share that was idle when the node was probed", () => {
  // NFS clients use x-systemd.automount, so df sees the share only while something is using it.
  const hardware = parseHardware([
    "cpu_cores|2",
    "arch|armv7l",
    "fs|mount=/ device=/dev/mmcblk0p1 type=ext4 size_gb=14.6",
    "netfs|mount=/mnt/ssd source=192.168.42.36:/mnt/ssd type=nfs",
    "discovered_at|2026-09-13T20:00:00Z",
  ].join("\n"));

  assert.deepEqual(hardware.networkMounts, [
    { mountpoint: "/mnt/ssd", source: "192.168.42.36:/mnt/ssd", fsType: "nfs", mounted: false },
  ]);

  const mounted = parseHardware([
    "cpu_cores|2",
    "arch|armv7l",
    "fs|mount=/ device=/dev/mmcblk0p1 type=ext4 size_gb=14.6",
    "fs|mount=/mnt/ssd device=192.168.42.36:/mnt/ssd type=nfs4 size_gb=116.8",
    "netfs|mount=/mnt/ssd source=192.168.42.36:/mnt/ssd type=nfs",
    "discovered_at|2026-09-13T20:00:00Z",
  ].join("\n"));
  assert.equal(mounted.networkMounts[0].mounted, true);
});

test("marks an MBR extended partition as a container rather than a sizeless unformatted partition", () => {
  // Debian's guided partitioning on MBR: root, an extended container, and swap logical inside it.
  const hardware = parseHardware([
    "cpu_cores|4",
    "arch|x86_64",
    'disk|NAME="sda" TYPE="disk" SIZE="34359738368" ROTA="1" RM="0" MODEL="QEMU HARDDISK"',
    'part|NAME="sda1" PKNAME="sda" TYPE="part" SIZE="32550944768" FSTYPE="ext4" LABEL="" MOUNTPOINT="/" PARTTYPE="0x83" PARTTYPENAME="Linux"',
    'part|NAME="sda2" PKNAME="sda" TYPE="part" SIZE="1024" FSTYPE="" LABEL="" MOUNTPOINT="" PARTTYPE="0xf" PARTTYPENAME="W95 Ext\'d (LBA)"',
    'part|NAME="sda5" PKNAME="sda" TYPE="part" SIZE="1805647872" FSTYPE="swap" LABEL="" MOUNTPOINT="[SWAP]" PARTTYPE="0x82" PARTTYPENAME="Linux swap / Solaris"',
    "root_device|/dev/sda1",
    "root_disk|sda",
    "discovered_at|2026-09-13T20:00:00Z",
  ].join("\n"));

  const [root, extended, swap] = hardware.disks[0].partitions;
  assert.equal(extended.container, true);
  assert.equal(extended.partitionType, "W95 Ext'd (LBA)");
  assert.equal(extended.fsType, undefined);

  // Only the extended container carries the flag; real partitions keep their filesystem detail.
  assert.equal(root.container, undefined);
  assert.equal(root.fsType, "ext4");
  assert.equal(root.mountpoint, "/");
  assert.equal(swap.container, undefined);
  assert.equal(swap.partitionType, "Linux swap / Solaris");
  assert.equal(swap.mountpoint, undefined, '"[SWAP]" is a state, not a mountpoint');
});