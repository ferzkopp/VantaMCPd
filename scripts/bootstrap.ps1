<#
.SYNOPSIS
    One-time bootstrap for a VantaMCPd cluster: deploy an SSH key and enable passwordless sudo.

.DESCRIPTION
    Requires a local inventory (cluster.config.local.json). If none exists the script offers to create
    one from cluster.config.example.json and stops so you can fill in your real hosts.

    Then, for each node:
      1. Installs the cluster public key into ~/.ssh/authorized_keys (you type the login password once per node).
      2. Installs /etc/sudoers.d/99-vanta granting NOPASSWD sudo to the SSH user (you type the sudo password once per node).
      3. Optionally installs a small baseline of admin tools.

    Your passwords are typed directly into ssh/sudo prompts - this script never reads, stores or forwards them.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1 -Nodes cluster4 -InstallBaseline
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1 -Verify
#>
[CmdletBinding()]
param(
    [string]   $ConfigPath,
    [string[]] $Nodes,
    [string]   $KeyPath,
    [switch]   $InstallBaseline,
    [switch]   $SkipKeyInstall,
    [switch]   $SkipSudoSetup,
    [switch]   $Verify
)

$ErrorActionPreference = 'Stop'
# $PSScriptRoot is empty inside param() defaults of an advanced script on PS 5.1.
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'common.ps1')

<#
Runs ssh and splits the result into stdout lines and raw stderr lines. Merging stderr into the pipeline
keeps the original message; letting it reach the error stream prints PowerShell's formatted ErrorRecord
instead. Not for calls that prompt for a password - use Invoke-Native so the prompt keeps the console.
#>
function Invoke-Ssh {
    param([Parameter(Mandatory = $true)][string[]] $SshArgs)
    $merged = @(Invoke-Native { & ssh @SshArgs 2>&1 })
    $code = $LASTEXITCODE
    return [pscustomobject]@{
        ExitCode = $code
        Output   = @($merged | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" })
        Stderr   = @($merged | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { $_.Exception.Message })
    }
}

foreach ($tool in 'ssh', 'ssh-keygen') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool not found. Install the Windows OpenSSH Client feature (Settings > System > Optional features)."
    }
}

# ------------------------------------------------------- local inventory gate
if (-not $ConfigPath) {
    $localConfig = Join-Path $repoRoot 'cluster.config.local.json'
    $exampleConfig = Join-Path $repoRoot 'cluster.config.example.json'

    if (-not (Test-Path $localConfig)) {
        Write-Step 'Local inventory required'
        Write-Host "    VantaMCPd never ships your real hosts. Before bootstrapping, create:" -ForegroundColor Yellow
        Write-Host "      $localConfig" -ForegroundColor Yellow
        Write-Host "    from the template:"
        Write-Host "      $exampleConfig"
        Write-Host ''
        if (-not (Test-Path $exampleConfig)) { throw "Template missing: $exampleConfig" }

        $answer = Read-Host 'Create it now from the template? [y/N]'
        if ($answer -notmatch '^(y|yes)$') {
            Write-Warn2 'Aborted. Create the inventory, then re-run this script.'
            exit 1
        }
        Copy-Item -Path $exampleConfig -Destination $localConfig
        Write-Ok "created $localConfig"
        Write-Host ''
        Write-Host 'Edit it now - set the real node names, hosts, roles and storage - then re-run this script.' -ForegroundColor Cyan
        Write-Host 'Nothing has been changed on any device.' -ForegroundColor Cyan
        if (Get-Command code -ErrorAction SilentlyContinue) { & code $localConfig }
        exit 1
    }
    $ConfigPath = $localConfig
}

if (-not (Test-Path $ConfigPath)) { throw "Config not found: $ConfigPath" }
$config = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json
Write-Step "Inventory: $ConfigPath"

$defaultUser = if ($config.defaults.user) { $config.defaults.user } else { 'configure' }
$defaultPort = if ($config.defaults.port) { [int]$config.defaults.port } else { 22 }
$rawKeyPath = if ($KeyPath) { $KeyPath } elseif ($config.defaults.privateKeyPath) { $config.defaults.privateKeyPath } else { '~/.ssh/vanta_cluster_ed25519' }
$keyFile = Expand-HomePath $rawKeyPath

$targets = $config.nodes
if ($Nodes) { $targets = $config.nodes | Where-Object { $Nodes -contains $_.name } }
if (-not $targets) { throw "No matching nodes. Available: $(($config.nodes | ForEach-Object name) -join ', ')" }

# ---------------------------------------------------------------- key material
Write-Step "SSH key: $keyFile"
$keyDir = Split-Path -Parent $keyFile
if (-not (Test-Path $keyDir)) { New-Item -ItemType Directory -Path $keyDir -Force | Out-Null }

if (-not (Test-Path $keyFile)) {
    if ($Verify) { throw "Key $keyFile does not exist. Run without -Verify first." }
    Write-Host "    Generating a new ed25519 key pair (no passphrase, for unattended agent use)..."
    Invoke-Native { & ssh-keygen -t ed25519 -a 100 -N '""' -C 'vantamcpd cluster' -f $keyFile } | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'ssh-keygen failed.' }
    Write-Ok 'key generated'
} else {
    Write-Ok 'key already exists'
}

$publicKey = (Get-Content -Raw -Path "$keyFile.pub").Trim()
if (-not $publicKey.StartsWith('ssh-')) { throw "Unexpected public key content in $keyFile.pub" }
Write-Host "    $publicKey"

# ---------------------------------------------------------------- remote scripts
$authorizedKeysScript = @"
set -e
umask 077
mkdir -p "`$HOME/.ssh"
chmod 700 "`$HOME/.ssh"
touch "`$HOME/.ssh/authorized_keys"
chmod 600 "`$HOME/.ssh/authorized_keys"
if grep -qxF '$publicKey' "`$HOME/.ssh/authorized_keys"; then
  echo "public key already present"
else
  printf '%s\n' '$publicKey' >> "`$HOME/.ssh/authorized_keys"
  echo "public key installed"
fi
"@

$baselineCmd = if ($InstallBaseline) {
    'export DEBIAN_FRONTEND=noninteractive; apt-get update -o DPkg::Lock::Timeout=300 >/dev/null 2>&1 || true; apt-get install -y -o DPkg::Lock::Timeout=300 usbutils lsb-release ca-certificates curl jq nfs-common >/dev/null 2>&1 && echo "baseline tools installed" || echo "baseline install had problems (continuing)"'
} else {
    'echo "baseline install skipped"'
}

$sudoersScript = @"
set -e
TARGET_USER="`$1"
[ -n "`$TARGET_USER" ] || { echo "no user supplied" >&2; exit 1; }
id "`$TARGET_USER" >/dev/null 2>&1 || { echo "user `$TARGET_USER does not exist" >&2; exit 1; }
TMP=`$(mktemp)
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "`$TARGET_USER" > "`$TMP"
visudo -c -f "`$TMP" >/dev/null
install -m 0440 -o root -g root "`$TMP" /etc/sudoers.d/99-vanta
rm -f "`$TMP"
echo "NOPASSWD sudo enabled for `$TARGET_USER"
grep -q '^PubkeyAuthentication' /etc/ssh/sshd_config || echo "PubkeyAuthentication defaults to yes"
$baselineCmd
echo "hostname: `$(hostname)"
echo "kernel:   `$(uname -srm)"
"@

$verifyScript = @"
echo "user=`$(id -un)"
echo "host=`$(hostname)"
echo "kernel=`$(uname -srm)"
if sudo -n true 2>/dev/null; then echo "sudo=nopasswd-ok"; else echo "sudo=NOT-CONFIGURED"; fi
echo "disk=`$(df -hP / | awk 'NR==2{print `$4" free of "`$2}')"
echo "mem=`$(free -m | awk '/^Mem:/{print `$7"MB available of "`$2"MB"}')"
"@

$b64AuthKeys = ConvertTo-Base64Script $authorizedKeysScript
$b64Sudoers  = ConvertTo-Base64Script $sudoersScript
$b64Verify   = ConvertTo-Base64Script $verifyScript

$sshCommon = @('-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=10')

$summary = @()

foreach ($node in $targets) {
    $nodeUser = if ($node.user) { $node.user } else { $defaultUser }
    $nodePort = if ($node.port) { [int]$node.port } else { $defaultPort }
    $dest = "$nodeUser@$($node.host)"
    $state = [ordered]@{ node = $node.name; host = $node.host; keyAuth = $false; nopasswdSudo = $false }

    Write-Host ''
    Write-Step "$($node.name)  ($dest`:$nodePort)"

    # 1. Key-based auth --------------------------------------------------------
    $keyProbe = @('-p', $nodePort, '-i', $keyFile, '-o', 'BatchMode=yes', '-o', 'PreferredAuthentications=publickey', $dest, 'true')
    $keyWorks = (Invoke-Ssh ($sshCommon + $keyProbe)).ExitCode -eq 0

    if ($keyWorks) {
        Write-Ok 'key-based login already working'
        $state.keyAuth = $true
    } elseif ($SkipKeyInstall -or $Verify) {
        Write-Warn2 'key-based login not working (install skipped)'
    } else {
        Write-Host "    Installing public key - you will be prompted for the $nodeUser password on $($node.host)." -ForegroundColor Yellow
        # No redirection here: ssh must keep the console for the password prompt.
        Invoke-Native { & ssh @sshCommon -p $nodePort $dest "echo $b64AuthKeys | base64 -d | bash -s" }
        if ($LASTEXITCODE -ne 0) { Write-Warn2 'key installation failed; skipping remaining steps for this node'; $summary += [pscustomobject]$state; continue }

        if ((Invoke-Ssh ($sshCommon + $keyProbe)).ExitCode -eq 0) { Write-Ok 'key-based login verified'; $state.keyAuth = $true }
        else { Write-Warn2 'key installed but key-based login still failing'; $summary += [pscustomobject]$state; continue }
    }

    # 2. Passwordless sudo -----------------------------------------------------
    $sudoProbe = @('-p', $nodePort, '-i', $keyFile, '-o', 'BatchMode=yes', $dest, 'sudo -n true')
    $sudoWorks = (Invoke-Ssh ($sshCommon + $sudoProbe)).ExitCode -eq 0

    if ($sudoWorks) {
        Write-Ok 'passwordless sudo already configured'
        $state.nopasswdSudo = $true
    } elseif ($SkipSudoSetup -or $Verify) {
        Write-Warn2 'passwordless sudo not configured (setup skipped)'
    } else {
        Write-Host "    Configuring NOPASSWD sudo - you will be prompted for the sudo password on $($node.host)." -ForegroundColor Yellow
        Invoke-Native { & ssh @sshCommon -tt -p $nodePort -i $keyFile $dest "echo $b64Sudoers | base64 -d | sudo -p 'sudo password for $($node.host): ' bash -s -- $nodeUser" }
        if ((Invoke-Ssh ($sshCommon + $sudoProbe)).ExitCode -eq 0) { Write-Ok 'passwordless sudo verified'; $state.nopasswdSudo = $true }
        else { Write-Warn2 'passwordless sudo still not working' }
    }

    # 3. Report ----------------------------------------------------------------
    if ($state.keyAuth) {
        $facts = Invoke-Ssh ($sshCommon + @('-p', $nodePort, '-i', $keyFile, '-o', 'BatchMode=yes', $dest, "echo $b64Verify | base64 -d | bash -s"))
        foreach ($line in $facts.Output) { if ($line) { Write-Host "    $line" -ForegroundColor DarkGray } }
        if ($facts.ExitCode -ne 0) { foreach ($line in $facts.Stderr) { Write-Warn2 $line } }
    }

    $summary += [pscustomobject]$state
}

Write-Host ''
Write-Step 'Summary'
$summary | Format-Table -AutoSize

# ------------------------------------------------- hardware inventory (shared code path with the daemon)
$reachable = @($summary | Where-Object { $_.keyAuth } | ForEach-Object node)
$discoverJs = Join-Path $repoRoot 'dist\discover.js'
if ($reachable -and -not $Verify) {
    Write-Host ''
    Write-Step 'Recording hardware (CPU, memory, disks, OS) in the inventory'
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Write-Warn2 'node not found - skipped. The daemon will fill this in on its first start.'
    } elseif (-not (Test-Path $discoverJs)) {
        Write-Warn2 'dist\discover.js missing - run "npm install; npm run build", then "npm run discover".'
    } else {
        $previousConfigEnv = $env:VANTA_CONFIG
        try {
            $env:VANTA_CONFIG = $ConfigPath
            # No output capture: --assign prompts interactively for disks it cannot classify.
            Invoke-Native { & node $discoverJs --assign @reachable }
        } finally {
            $env:VANTA_CONFIG = $previousConfigEnv
        }
    }
}

$bad = $summary | Where-Object { -not $_.keyAuth -or -not $_.nopasswdSudo }
if ($bad) {
    Write-Warn2 "Nodes needing attention: $(($bad | ForEach-Object node) -join ', ')"
    Write-Host '    Re-run this script, or fix manually with:  ssh <user>@<host>  then  sudo visudo -f /etc/sudoers.d/99-vanta'
} else {
    Write-Host ''
    Write-Host 'All nodes ready. Next:' -ForegroundColor Green
    Write-Host '  powershell -ExecutionPolicy Bypass -File scripts/prepare-nodes.ps1'
    Write-Host '  npm install; npm run build'
    Write-Host '  Then reload the MCP server in VS Code (Command Palette > MCP: List Servers > vanta > Restart).'
}
