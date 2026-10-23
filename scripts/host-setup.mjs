import { spawnSync } from "node:child_process";
import { chmodSync, constants, copyFileSync, existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const helpers = { prereqs: "install-prereqs", bootstrap: "bootstrap", "prepare-nodes": "prepare-nodes", sources: "smb-source" };

export function windowsInvocation(action, args) {
  if (!Object.hasOwn(helpers, action)) throw new Error("Unknown setup action.");
  const names = { check: "Check", verify: "Verify", nodes: "Nodes", config: "ConfigPath", "config-path": "ConfigPath",
    "key-path": "KeyPath", "source-ids": "SourceIds", "verify-sources": "VerifySources", "configure-sources": "ConfigureSources",
    "extra-packages": "ExtraPackages", "install-baseline": "InstallBaseline", "reset-host-key": "ResetHostKey" };
  return ["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path.join(root, "scripts", `${helpers[action]}.ps1`),
    ...args.map((argument) => argument.startsWith("--") && names[argument.slice(2)] ? `-${names[argument.slice(2)]}` : argument)];
}

export function linuxOptions(args, action) {
  const options = {};
  const flags = new Set(["check", "verify", "verify-sources", "install-baseline"]);
  const valued = new Set(["config", "config-path", "nodes", "extra-packages", "source-ids"]);
  for (let index = 0; index < args.length; index++) {
    const name = args[index].replace(/^--/, "");
    if (!args[index].startsWith("--") || (!flags.has(name) && !valued.has(name))) throw new Error(`Unsupported Linux option: ${args[index]}`);
    if (flags.has(name)) options[name] = true;
    else {
      const value = args[++index];
      if (!value || value.startsWith("--")) throw new Error(`--${name} requires a value.`);
      options[name === "config-path" ? "config" : name] = value;
    }
  }
  const allowed = {
    prereqs: ["check"],
    bootstrap: ["config", "nodes", "verify", "verify-sources", "source-ids", "install-baseline"],
    "prepare-nodes": ["config", "nodes", "check", "extra-packages"],
    sources: ["config", "verify", "source-ids"],
  };
  if (action && Object.keys(options).some((name) => !allowed[action]?.includes(name))) {
    throw new Error(`Unsupported option for ${action}; use --help and scripts/README.md.`);
  }
  if (options.verify && options["install-baseline"]) throw new Error("--verify cannot install baseline packages.");
  return options;
}

function run(command, args, extra = {}) {
  const result = spawnSync(command, args, { cwd: root, stdio: "inherit", ...extra });
  if (result.error || result.signal || result.status !== 0) throw new Error(`${command} failed${result.status === null ? "" : ` (exit ${result.status})`}.`);
}

function prerequisites(check) {
  const [major, minor] = process.versions.node.split(".").map(Number);
  if (major < 20 || (major === 20 && minor < 11)) throw new Error("Node.js 20.11 or newer is required.");
  for (const command of ["npm", "ssh", "ssh-keygen", "ssh-copy-id"]) {
    const found = spawnSync("/bin/sh", ["-c", 'command -v "$1"', "sh", command], { stdio: "ignore" });
    if (found.status !== 0) throw new Error(`Install ${command} using your host's package manager (OpenSSH client package for SSH tools), then rerun. No host packages are installed automatically.`);
  }
  console.log("Node.js, npm and OpenSSH client prerequisites found.");
  if (!check) { run("npm", ["install"]); run("npm", ["run", "build"]); }
}

async function inventory(options, create) {
  const filename = path.resolve(options.config ?? process.env.VANTA_CONFIG ?? path.join(root, "cluster.config.local.json"));
  if (!existsSync(filename) && create && !options.config && !process.env.VANTA_CONFIG) {
    copyFileSync(path.join(root, "cluster.config.example.json"), filename, constants.COPYFILE_EXCL);
    chmodSync(filename, 0o600);
    throw new Error("Created cluster.config.local.json. Edit its node addresses, user and roles, then rerun bootstrap.");
  }
  if (!existsSync(path.join(root, "dist", "config.js"))) throw new Error("Run npm install and npm run build before enrollment or source verification.");
  const { loadConfig } = await import("../dist/config.js");
  return loadConfig(filename);
}

function targets(config, names) {
  const requested = names ? names.split(",") : config.nodes.map((node) => node.name);
  return requested.map((name) => {
    const node = config.nodes.find((candidate) => candidate.name === name);
    if (!node) throw new Error(`Unknown node: ${name}`);
    if (!/^[A-Za-z_][A-Za-z0-9_.-]*\$?$/.test(node.user) || !/^[A-Za-z0-9][A-Za-z0-9_.:%-]*$/.test(node.host)) {
      throw new Error(`Node ${name} requires a conventional SSH username and hostname/address for setup helpers.`);
    }
    return node;
  });
}

function sshArgs(node, verify = false) {
  if (node.auth !== "key" || !node.privateKeyPath) throw new Error(`Node ${node.name}: setup requires key authentication and privateKeyPath.`);
  return ["-p", String(node.port), "-i", node.privateKeyPath, "-o", `StrictHostKeyChecking=${verify ? "yes" : "ask"}`,
    "-o", "ConnectTimeout=10", ...(verify ? ["-o", "BatchMode=yes"] : [])];
}

async function verifySources(config, ids, advisory = false) {
  const { SourceClient } = await import("../dist/sources.js");
  const client = new SourceClient(config);
  const requested = ids?.split(",");
  if (requested?.some((id) => !config.sources.some((source) => source.id === id && source.enabled))) throw new Error("Unknown or disabled source ID.");
  let failed = false;
  for (const source of config.sources.filter((candidate) => candidate.enabled && (!requested || requested.includes(candidate.id)))) {
    const result = await client.check(source.id);
    console.log(`${source.id}: ${result.message}`);
    for (const message of result.guidance) console.log(`  ${message}`);
    failed ||= !result.ok;
  }
  if (failed && !advisory) throw new Error("SMB source verification failed. See docs/Sources.md for Linux mount setup.");
}

const sudoRule = 'set -eu; temporary=$(mktemp); trap \'rm -f "$temporary"\' EXIT; ' +
  'printf "%s ALL=(ALL) NOPASSWD:ALL\\n" "$(id -un)" > "$temporary"; ' +
  'sudo visudo -cf "$temporary"; sudo install -m 0440 -o root -g root "$temporary" /etc/sudoers.d/99-vanta';

export const preparationScript = `set -eu
mode="$1"; shift
hostname; uname -srm; df -hP /; free -m
missing=""
for package in "$@"; do
  if ! dpkg-query -W -f='\${Status}' "$package" 2>/dev/null | grep -qx 'install ok installed'; then
    missing="$missing $package"
  fi
done
printf 'Missing packages:%s\\n' "$missing"
[ -n "$missing" ] || exit 0
[ "$mode" = install ] || exit 0
sudo -n true
sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -q -o DPkg::Lock::Timeout=300 </dev/null
sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -q -o Dpkg::Use-Pty=0 -o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -- $missing </dev/null
`;

function prepare(nodes, options) {
  const baseline = ["ca-certificates", "curl", "jq", "lsb-release", "procps", "iproute2", "usbutils", "nfs-common", "parted"];
  const extra = options["extra-packages"]?.split(",") ?? [];
  if (extra.some((name) => !/^[a-z0-9][a-z0-9+.-]*$/.test(name))) throw new Error("Extra packages must be comma-separated Debian package names.");
  for (const node of nodes) {
    const packages = [...baseline, ...(node.storage ? ["e2fsprogs", "nfs-kernel-server", "smartmontools"] : []), ...extra];
    run("ssh", [...sshArgs(node, true), `${node.user}@${node.host}`, `bash -s -- ${options.check ? "check" : "install"} ${packages.join(" ")}`],
      { input: preparationScript, stdio: ["pipe", "inherit", "inherit"] });
  }
}

export async function main([action, ...args]) {
  if (!Object.hasOwn(helpers, action)) throw new Error("Use prereqs, bootstrap, prepare-nodes or sources.");
  if (args.includes("--help")) {
    console.log(`Usage: node scripts/host-setup.mjs ${action} [options]\nLinux: --config PATH --nodes NAME[,NAME] --check --verify --verify-sources --source-ids ID[,ID] --extra-packages PACKAGE[,PACKAGE] --install-baseline\nWindows: delegates to scripts/${helpers[action]}.ps1; native PowerShell flags are also accepted.\nSee scripts/README.md and docs/HostSetup.md. Linux SMB mounts and credentials are administrator-managed.`);
    return;
  }
  if (process.platform === "win32") { run("powershell.exe", windowsInvocation(action, args)); return; }
  if (process.platform !== "linux") throw new Error("Setup helpers support Windows and Linux hosts.");
  const options = linuxOptions(args, action);
  if (action === "prereqs") { prerequisites(options.check); return; }
  const config = await inventory(options, action === "bootstrap" && !options.verify && !options["verify-sources"]);
  if (action === "sources" || options["verify-sources"]) { await verifySources(config, options["source-ids"]); return; }
  const nodes = targets(config, options.nodes);
  if (action === "prepare-nodes") {
    if (nodes.some((node) => node.storage)) await verifySources(config, undefined, true);
    prepare(nodes, options); return;
  }
  for (const node of nodes) {
    const common = sshArgs(node, options.verify);
    const destination = `${node.user}@${node.host}`;
    console.log(`\n${node.name}: ${destination}`);
    if (!options.verify) {
      if (!existsSync(node.privateKeyPath)) {
        mkdirSync(path.dirname(node.privateKeyPath), { recursive: true, mode: 0o700 });
        run("ssh-keygen", ["-t", "ed25519", "-a", "100", "-N", "", "-C", "vantamcpd cluster", "-f", node.privateKeyPath]);
      }
      if (!existsSync(`${node.privateKeyPath}.pub`)) throw new Error("The existing private key has no .pub file. Recover its public key yourself; no key will be overwritten.");
      run("ssh-copy-id", ["-i", `${node.privateKeyPath}.pub`, "-p", String(node.port), "-o", "StrictHostKeyChecking=ask", "-o", "ConnectTimeout=10", destination]);
      run("ssh", [...common, "-t", destination, sudoRule]);
    }
    run("ssh", [...sshArgs(node, true), destination, "set -e; id -un; hostname; sudo -n true; uname -srm"]);
  }
  if (!options.verify) {
    run(process.execPath, [path.join(root, "dist", "discover.js"), "--assign", ...nodes.map((node) => node.name)],
      { env: { ...process.env, VANTA_CONFIG: config.configPath } });
    if (options["install-baseline"]) prepare(nodes, options);
    await verifySources(config, undefined, true);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch((error) => { console.error(error.message); process.exitCode = 1; });
}