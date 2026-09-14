/**
 * One canonical shape for every remote apt-get invocation, so unattended behaviour cannot drift
 * between the package tool, module dependency installation and the storage tool.
 */

/** Never prompt, never allocate a pty, wait for the dpkg lock, and keep the existing config files. */
const APT_OPTIONS = [
  "-y",
  "-q",
  "-o Dpkg::Use-Pty=0",
  "-o DPkg::Lock::Timeout=300",
  "-o Dpkg::Options::=--force-confdef",
  "-o Dpkg::Options::=--force-confold",
].join(" ");

/**
 * sudo resets the environment, so these must travel with the command itself rather than an earlier
 * `export`. NEEDRESTART_MODE keeps needrestart from opening its curses dialog.
 */
const APT_ENV = "env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a";

export interface AptOptions {
  /** Package names, already validated and shell-quoted by the caller. */
  packages?: string[];
  /** Simulate with -s instead of changing anything. */
  dryRun?: boolean;
  /** Extra apt-get flags, e.g. "--reinstall" or "--no-upgrade". */
  flags?: string[];
  /** Discard apt's own output, for a preparatory `update` in front of the real command. */
  quiet?: boolean;
}

/**
 * Build one apt-get command line. stdin comes from /dev/null because the surrounding script is itself
 * being read from the SSH stdin pipe: a child that reads stdin would swallow the rest of the script.
 */
export function aptGet(subcommand: string, options: AptOptions = {}): string {
  const { packages = [], dryRun = false, flags = [], quiet = false } = options;
  const parts = [APT_ENV, "apt-get", ...(dryRun ? ["-s"] : []), APT_OPTIONS, ...flags, subcommand];
  if (packages.length > 0) parts.push("--", ...packages);
  const command = parts.join(" ");
  return quiet ? `${command} >/dev/null 2>&1 </dev/null` : `${command} </dev/null`;
}

/** Refresh the package indexes. Used on its own and as the preparatory step of install/upgrade. */
export function aptUpdate(quiet = false): string {
  return aptGet("update", { quiet });
}
