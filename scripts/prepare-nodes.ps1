<#
.SYNOPSIS
    Installs the packages VantaMCPd's tools rely on onto freshly imaged cluster nodes.

.DESCRIPTION
    Run after scripts/bootstrap.ps1 (which creates the local inventory, deploys the SSH key and the
    NOPASSWD sudo rule). For every selected node it reports OS, architecture, free disk/memory, time
    sync and reboot-required state, then installs any missing baseline package.

    Baseline (all nodes):  ca-certificates curl jq lsb-release procps iproute2 usbutils nfs-common
    Nodes whose role includes "storage" also get: e2fsprogs parted nfs-kernel-server smartmontools

    Idempotent - packages already installed are never touched, and no upgrade is performed.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/prepare-nodes.ps1 -Check
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/prepare-nodes.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/prepare-nodes.ps1 -Nodes cluster4 -ExtraPackages tmux
#>
[CmdletBinding()]
param(
    [string]   $ConfigPath,
    [string[]] $Nodes,
    [string[]] $ExtraPackages = @(),
    [string]   $KeyPath,
    [switch]   $Check
)

$ErrorActionPreference = 'Stop'
# $PSScriptRoot is empty inside param() defaults of an advanced script on PS 5.1.
if (-not $ConfigPath) { $ConfigPath = Join-Path $PSScriptRoot '..\cluster.config.local.json' }
. (Join-Path $PSScriptRoot 'common.ps1')

$baselinePackages = @('ca-certificates', 'curl', 'jq', 'lsb-release', 'procps', 'iproute2', 'usbutils', 'nfs-common', 'parted')
$storagePackages = @('e2fsprogs', 'nfs-kernel-server', 'smartmontools')

foreach ($tool in 'ssh') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool not found. Run scripts/install-prereqs.ps1 first."
    }
}

if (-not (Test-Path $ConfigPath)) { throw "Inventory not found: $ConfigPath. Run scripts/bootstrap.ps1 first - it creates one from cluster.config.example.json." }
$config = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json

$defaultUser = if ($config.defaults.user) { $config.defaults.user } else { 'configure' }
$defaultPort = if ($config.defaults.port) { [int]$config.defaults.port } else { 22 }
$rawKeyPath = if ($KeyPath) { $KeyPath } elseif ($config.defaults.privateKeyPath) { $config.defaults.privateKeyPath } else { '~/.ssh/vanta_cluster_ed25519' }
$keyFile = Expand-HomePath $rawKeyPath
if (-not (Test-Path $keyFile)) { throw "Private key not found: $keyFile. Run scripts/bootstrap.ps1 first." }

$targets = $config.nodes
if ($Nodes) { $targets = $config.nodes | Where-Object { $Nodes -contains $_.name } }
if (-not $targets) { throw "No matching nodes. Available: $(($config.nodes | ForEach-Object name) -join ', ')" }

# Single-quoted here-string: everything below is evaluated by bash on the node, not by PowerShell.
# argv: <check|install> <package> [package ...]
$remoteScript = @'
set -u
MODE="$1"; shift
PKGS="$*"

missing=""
for p in $PKGS; do
  if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed"; then
    :
  else
    missing="$missing $p"
  fi
done
missing="${missing# }"

echo "host=$(hostname)"
if [ -r /etc/os-release ]; then echo "os=$(. /etc/os-release; echo "$PRETTY_NAME")"; fi
echo "arch=$(dpkg --print-architecture) kernel=$(uname -r)"
echo "disk_free=$(df -hP / | awk 'NR==2{print $4" of "$2}')"
echo "mem_available=$(free -m | awk '/^Mem:/{print $7"MB of "$2"MB"}')"
echo "time_synced=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
if [ -f /var/run/reboot-required ]; then echo "reboot_required=yes"; else echo "reboot_required=no"; fi
if sudo -n true 2>/dev/null; then echo "sudo=nopasswd-ok"; else echo "sudo=NOT-CONFIGURED"; fi

if [ -z "$missing" ]; then
  echo "packages=all present"
  exit 0
fi
echo "packages_missing=$missing"
[ "$MODE" = "install" ] || exit 10

if ! sudo -n true 2>/dev/null; then
  echo "passwordless sudo unavailable - run scripts/bootstrap.ps1 for this node" >&2
  exit 11
fi

export DEBIAN_FRONTEND=noninteractive
# The same non-interactive flags the daemon uses at runtime; see src/apt.ts.
APT_OPTS="-y -q -o Dpkg::Use-Pty=0 -o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
# sudo resets the environment, so DEBIAN_FRONTEND must be re-applied on the sudo side via env(1),
# and stdin must come from /dev/null - bash is reading this very script from the pipe.
echo "--- apt-get update"
# shellcheck disable=SC2086
sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update $APT_OPTS </dev/null ||
  echo "apt-get update reported errors (continuing)"
echo "--- apt-get install $missing"
# shellcheck disable=SC2086
sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install $APT_OPTS -- $missing </dev/null || exit 12
echo "packages=installed"
'@

$b64Script = ConvertTo-Base64Script $remoteScript

$mode = if ($Check) { 'check' } else { 'install' }
$sshCommon = @('-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes')
$summary = @()

Write-Step "Baseline: $($baselinePackages -join ' ')"
Write-Step "Storage roles also: $($storagePackages -join ' ')"
if ($ExtraPackages) { Write-Step "Extra: $($ExtraPackages -join ' ')" }
if ($Check) { Write-Host '    -Check: reporting only, nothing will be installed.' -ForegroundColor DarkGray }

foreach ($node in $targets) {
    $nodeUser = if ($node.user) { $node.user } else { $defaultUser }
    $nodePort = if ($node.port) { [int]$node.port } else { $defaultPort }
    $dest = "$nodeUser@$($node.host)"

    $packages = @($baselinePackages)
    $role = if ($node.role) { $node.role } else { 'worker' }
    if ($role -like '*storage*' -or ($node.tags -contains 'storage')) { $packages += $storagePackages }
    $packages += $ExtraPackages
    $packages = $packages | Select-Object -Unique

    # Package names come from this script and the config file; validate anyway - they are passed to apt.
    $bad = @($packages | Where-Object { $_ -notmatch '^[a-z0-9][a-z0-9+._-]*$' })
    if ($bad) { throw "Invalid package name(s): $($bad -join ', ')" }

    Write-Host ''
    Write-Step "$($node.name)  [$role]  ($dest`:$nodePort)"

    $state = [ordered]@{ node = $node.name; host = $node.host; role = $role; result = 'unknown' }

    # Merging stderr into the pipeline keeps the raw message; letting it reach the error stream would
    # print PowerShell's formatted ErrorRecord instead ("ssh.exe : ...", "At line:...").
    $merged = @(Invoke-Native { & ssh @sshCommon -p $nodePort -i $keyFile $dest "echo $b64Script | base64 -d | bash -s -- $mode $($packages -join ' ')" 2>&1 })
    $code = $LASTEXITCODE
    # apt can repeat "Waiting for cache lock" a hundred times; collapse consecutive duplicates.
    $previousLine = $null
    $repeatCount = 0
    foreach ($item in $merged) {
        $isErr = $item -is [System.Management.Automation.ErrorRecord]
        $line = if ($isErr) { $item.Exception.Message } else { "$item" }
        if ($line -eq $previousLine) { $repeatCount++; continue }
        if ($repeatCount -gt 0) { Write-Host "      ... repeated $repeatCount more time(s)" -ForegroundColor DarkGray }
        $repeatCount = 0
        Write-Host "    $line" -ForegroundColor $(if ($isErr) { 'Yellow' } else { 'DarkGray' })
        $previousLine = $line
    }
    if ($repeatCount -gt 0) { Write-Host "      ... repeated $repeatCount more time(s)" -ForegroundColor DarkGray }

    switch ($code) {
        0  { if ($Check) { Write-Ok 'all prerequisites present' } else { Write-Ok 'prerequisites in place' }; $state.result = 'ok' }
        10 { Write-Warn2 'packages missing - re-run without -Check to install'; $state.result = 'missing-packages' }
        11 { Write-Fail 'passwordless sudo not configured - run scripts/bootstrap.ps1'; $state.result = 'no-sudo' }
        12 { Write-Fail 'apt-get install failed - see output above'; $state.result = 'apt-failed' }
        255 { Write-Fail 'SSH connection or key authentication failed - run scripts/bootstrap.ps1'; $state.result = 'unreachable' }
        default { Write-Fail "remote script exited with $code"; $state.result = "exit-$code" }
    }

    $summary += [pscustomobject]$state
}

Write-Host ''
Write-Step 'Summary'
$summary | Format-Table -AutoSize

if (@($summary | Where-Object { $_.result -ne 'ok' })) { exit 1 }
exit 0
