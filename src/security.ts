import type { SecurityConfig } from "./config.js";
import { isIP } from "node:net";

/** POSIX single-quote a string so it is safe to embed in a shell command. */
export function q(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

export function assertNoControlChars(value: string, what: string): void {
  // eslint-disable-next-line no-control-regex
  if (/[\u0000\r\n]/.test(value)) throw new Error(`${what} must not contain newlines or NUL bytes.`);
}

const PACKAGE_RE = /^[a-z0-9][a-z0-9+._-]*(?:[:=][A-Za-z0-9+.~:_-]+)?$/i;
const UNIT_RE = /^[A-Za-z0-9@._\\:-]+$/;
const ABS_PATH_RE = /^\/[^\u0000\n\r]*$/;
const DEVICE_RE = /^\/dev\/[A-Za-z0-9\/_-]+$/;
const CIDR_PREFIX_RE = /^(?:[0-9]|[12][0-9]|3[0-2])$/;
const MOUNT_OPTS_RE = /^[A-Za-z0-9=,._:+-]+$/;

export function validatePackage(name: string): string {
  if (!PACKAGE_RE.test(name)) throw new Error(`Invalid package name: ${JSON.stringify(name)}`);
  return name;
}

export function validateUnit(name: string): string {
  if (!UNIT_RE.test(name)) throw new Error(`Invalid systemd unit name: ${JSON.stringify(name)}`);
  return name;
}

export function validateAbsPath(p: string, what = "path"): string {
  if (!ABS_PATH_RE.test(p)) throw new Error(`${what} must be an absolute POSIX path without newlines: ${JSON.stringify(p)}`);
  return p;
}

export function validateDevice(dev: string): string {
  if (!DEVICE_RE.test(dev)) throw new Error(`Invalid block device: ${JSON.stringify(dev)}`);
  return dev;
}

export function validateCidr(cidr: string): string {
  const [address, prefix, ...extra] = cidr.split("/");
  if (isIP(address ?? "") !== 4 || extra.length > 0 || (prefix !== undefined && !CIDR_PREFIX_RE.test(prefix))) {
    throw new Error(`Invalid network/CIDR: ${JSON.stringify(cidr)}`);
  }
  return cidr;
}

export function validateMountOptions(opts: string): string {
  if (!MOUNT_OPTS_RE.test(opts)) throw new Error(`Invalid mount/export options: ${JSON.stringify(opts)}`);
  return opts;
}

interface DangerRule {
  pattern: RegExp;
  reason: string;
}

const DANGEROUS_RULES: DangerRule[] = [
  { pattern: /\brm\b[^\n]*\s-[a-zA-Z]*[rR][a-zA-Z]*f|\brm\b[^\n]*\s-[a-zA-Z]*f[a-zA-Z]*[rR]/, reason: "recursive forced delete" },
  { pattern: /\brm\b[^\n]*\s\/(?:\s|$)/, reason: "delete targeting /" },
  { pattern: /\bmkfs(\.\w+)?\b/, reason: "filesystem creation (destroys data)" },
  { pattern: /\b(wipefs|sgdisk|fdisk|parted|gdisk|cfdisk)\b/, reason: "partition table modification" },
  { pattern: /\bdd\b[^\n]*\bof=\s*\/dev\//, reason: "raw write to block device" },
  { pattern: />\s*\/dev\/(?:sd[a-z]|mmcblk|nvme)/, reason: "redirect onto block device" },
  { pattern: /\b(shutdown|poweroff|halt|reboot)\b/, reason: "power state change" },
  { pattern: /\binit\s+[06]\b/, reason: "runlevel change" },
  { pattern: /:\s*\(\s*\)\s*\{[^}]*\}\s*;\s*:/, reason: "fork bomb" },
  { pattern: /\b(userdel|groupdel|deluser)\b/, reason: "account deletion" },
  { pattern: /\bpasswd\b(?!\s*-S)/, reason: "credential modification" },
  { pattern: /\b(chmod|chown)\b[^\n]*\s-R[^\n]*\s\/(?:\s|$|etc|boot|usr|var|bin|sbin|lib)/, reason: "recursive permission change on system paths" },
  { pattern: /\/etc\/(sudoers|shadow|passwd)\b[^\n]*(>|tee|sed\s+-i|rm\b)/, reason: "modification of auth databases" },
  { pattern: /\bapt(-get)?\b[^\n]*\b(remove|purge)\b[^\n]*\b(systemd|openssh-server|linux-image|libc6|initramfs-tools)\b/, reason: "removal of critical packages" },
  { pattern: /\bufw\s+(disable|reset)\b|\biptables\s+-F\b|\bnft\s+flush\b/, reason: "firewall teardown" },
  { pattern: /\bcryptsetup\b/, reason: "disk encryption change" },
  { pattern: /\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(ba)?sh\b/, reason: "pipe-from-internet to shell" },
  { pattern: /\bhistory\s+-c\b|\bshred\b/, reason: "anti-forensic / destructive" },
  { pattern: />\s*\/etc\/fstab\b/, reason: "overwrite of fstab (can make node unbootable)" },
];

export interface DangerAssessment {
  dangerous: boolean;
  reasons: string[];
}

export function assessCommand(command: string, security: SecurityConfig): DangerAssessment {
  const reasons: string[] = [];
  for (const rule of DANGEROUS_RULES) {
    if (rule.pattern.test(command)) reasons.push(rule.reason);
  }
  for (const extra of security.extraDenyPatterns) {
    try {
      if (new RegExp(extra, "i").test(command)) reasons.push(`matches configured deny pattern /${extra}/`);
    } catch {
      reasons.push(`configured deny pattern /${extra}/ is not a valid regular expression`);
    }
  }
  return { dangerous: reasons.length > 0, reasons: [...new Set(reasons)] };
}

/** Throws unless the caller has explicitly acknowledged a destructive command. */
export function guardCommand(command: string, security: SecurityConfig, confirmed: boolean): DangerAssessment {
  if (!security.allowArbitraryCommands) {
    throw new Error("Arbitrary command execution is disabled (security.allowArbitraryCommands = false).");
  }
  const assessment = assessCommand(command, security);
  if (assessment.dangerous && security.requireConfirmForDangerous && !confirmed) {
    throw new Error(
      `Refusing to run a potentially destructive command without confirmation.\n` +
        `Reasons: ${assessment.reasons.join("; ")}\n` +
        `Command: ${command}\n` +
        `Ask the user to approve, then re-run with confirm: true.`,
    );
  }
  return assessment;
}

/** Block devices that are almost certainly the boot/root media on these Armbian SBCs. */
export function guardDestructiveDevice(device: string): void {
  validateDevice(device);
  if (/^\/dev\/(mmcblk|ram|loop)/.test(device)) {
    throw new Error(`Refusing to operate on ${device}: this is the SD card / boot media on these nodes.`);
  }
  if (/^\/dev\/(sd[a-z]|nvme\d+n\d+)$/.test(device) === false && /^\/dev\/(sd[a-z]\d+|nvme\d+n\d+p\d+)$/.test(device) === false) {
    throw new Error(`Refusing to operate on ${device}: expected a USB/NVMe disk or partition such as /dev/sda or /dev/sda1.`);
  }
}
