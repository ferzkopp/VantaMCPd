import type { AcceleratorInfo, ResolvedNode } from "../config.js";
import type { AcceleratorRequirement, ModuleManifest } from "./manifest.js";

export type CompatibilityStatus = "compatible" | "incompatible" | "unknown";

export interface CompatibilityResult {
  status: CompatibilityStatus;
  reasons: string[];
  unknown: string[];
}

function normalize(value: string): string {
  return value.trim().toLowerCase();
}

function versionParts(value: string): number[] {
  return value.split(/[^0-9]+/).filter(Boolean).map(Number);
}

function versionAtLeast(actual: string, minimum: string): boolean {
  const left = versionParts(actual);
  const right = versionParts(minimum);
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0);
    if (difference !== 0) return difference > 0;
  }
  return true;
}

function acceleratorMatches(actual: AcceleratorInfo, required: AcceleratorRequirement): boolean {
  if (normalize(actual.kind) !== normalize(required.kind)) return false;
  if (required.vendor && normalize(actual.vendor ?? "") !== normalize(required.vendor)) return false;
  if (required.model && !normalize(actual.model ?? "").includes(normalize(required.model))) return false;
  if (required.minMemoryMb !== undefined && (actual.memoryMb ?? -1) < required.minMemoryMb) return false;
  if (required.runtime && normalize(actual.runtime ?? "") !== normalize(required.runtime)) return false;
  if (required.minRuntimeVersion && (!actual.runtimeVersion || !versionAtLeast(actual.runtimeVersion, required.minRuntimeVersion))) {
    return false;
  }
  return true;
}

export function evaluateCompatibility(manifest: ModuleManifest, node: ResolvedNode): CompatibilityResult {
  const requirements = manifest.compatibility;
  const hardware = node.hardware;
  const reasons: string[] = [];
  const unknown: string[] = [];

  if (!hardware) {
    return { status: "unknown", reasons, unknown: ["hardware inventory has not been discovered"] };
  }

  if (requirements.os) {
    const actual = hardware.os?.id;
    if (!actual) unknown.push("operating system ID is not recorded");
    else if (!requirements.os.map(normalize).includes(normalize(actual))) reasons.push(`OS ${actual} is not supported`);
  }

  if (requirements.architectures) {
    const actual = hardware.cpu?.packageArch ?? hardware.cpu?.arch;
    if (!actual) unknown.push("CPU/package architecture is not recorded");
    else if (!requirements.architectures.map(normalize).includes(normalize(actual))) {
      reasons.push(`architecture ${actual} is not supported`);
    }
  }

  if (requirements.minCores !== undefined) {
    const actual = hardware.cpu?.cores;
    if (actual === undefined) unknown.push("CPU core count is not recorded");
    else if (actual < requirements.minCores) reasons.push(`requires ${requirements.minCores} CPU cores; node has ${actual}`);
  }

  if (requirements.minRamMb !== undefined) {
    const actual = hardware.memory?.totalMb;
    if (actual === undefined) unknown.push("total memory is not recorded");
    else if (actual < requirements.minRamMb) reasons.push(`requires ${requirements.minRamMb} MB RAM; node has ${actual} MB`);
  }

  if (manifest.persistentData?.storage === "node") {
    if (!node.storage) {
      reasons.push("requires configured node-local storage");
    } else if (hardware.filesystems === undefined) {
      unknown.push("filesystem inventory is not recorded");
    } else if (!hardware.filesystems.some((filesystem) => filesystem.mountpoint === node.storage?.mountpoint)) {
      unknown.push(`configured storage mount ${node.storage.mountpoint} is not present in recorded inventory`);
    }
  }

  if (requirements.accelerators.length > 0) {
    if (hardware.accelerators === undefined) {
      unknown.push("accelerator inventory is not recorded");
    } else {
      for (const required of requirements.accelerators) {
        if (!hardware.accelerators.some((actual) => acceleratorMatches(actual, required))) {
          const detail = [required.vendor, required.model, required.kind].filter(Boolean).join(" ");
          reasons.push(`required accelerator is unavailable: ${detail}`);
        }
      }
    }
  }

  return {
    status: reasons.length > 0 ? "incompatible" : unknown.length > 0 ? "unknown" : "compatible",
    reasons,
    unknown,
  };
}