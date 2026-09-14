import assert from "node:assert/strict";
import test from "node:test";

import { parseHardware } from "../dist/hardware.js";

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