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

    A reimaged node presents a new host key, which both trust stores reject as a possible interception.
    -ResetHostKey forgets the recorded key for the target nodes first. Only use it when you know the node
    was reinstalled: it discards the evidence that would expose a man-in-the-middle.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1 -Nodes cluster4 -InstallBaseline
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1 -Nodes cluster6 -ResetHostKey
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
    [switch]   $ResetHostKey,
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

<#
Forgets a node's recorded host key in both trust stores: the OpenSSH client's known_hosts (used by this
script) and ~/.vanta/known_hosts.json (used by the daemon). Only ever called behind -ResetHostKey - a
changed host key is indistinguishable from an interception, so it must never be discarded automatically.
#>
function Reset-KnownHostKey {
    param(
        [Parameter(Mandatory = $true)][string] $HostName,
        [int] $Port = 22
    )

    $sshKnownHosts = Join-Path $HOME '.ssh\known_hosts'
    if (Test-Path $sshKnownHosts) {
        # A non-default port is recorded as [host]:port, so both spellings have to go.
        $ids = @($HostName)
        if ($Port -ne 22) { $ids += "[$HostName]:$Port" }
        foreach ($id in $ids) {
            $output = @(Invoke-Native { & ssh-keygen -R $id -f $sshKnownHosts 2>&1 } | ForEach-Object { "$_" })
            foreach ($line in $output) {
                if ($line -match 'found: line (\d+)') { Write-Ok "removed $id from known_hosts (line $($Matches[1]))" }
            }
        }
    }

    $vantaStore = if ($env:VANTA_KNOWN_HOSTS) { Expand-HomePath $env:VANTA_KNOWN_HOSTS } else { Join-Path $HOME '.vanta\known_hosts.json' }
    if (Test-Path $vantaStore) {
        $id = "${HostName}:${Port}"
        try {
            $store = (Get-Content -Raw -Path $vantaStore) -replace '^\uFEFF', '' | ConvertFrom-Json
            if ($store.PSObject.Properties.Name -contains $id) {
                Write-Ok "removed $id from $vantaStore (was $($store.$id))"
                $store.PSObject.Properties.Remove($id)
                $json = $store | ConvertTo-Json -Depth 5
                if (-not $json) { $json = '{}' }
                [IO.File]::WriteAllText($vantaStore, $json, (New-Object Text.UTF8Encoding($false)))
            }
        } catch {
            Write-Warn2 "could not update $vantaStore ($($_.Exception.Message)); the daemon re-pins on next connect if you delete it"
        }
    }

    # Print what the node offers now, so it can be compared against the node's own console.
    $scanned = @(Invoke-Native { & ssh-keyscan -T 5 -p $Port $HostName 2>$null } | ForEach-Object { "$_" } | Where-Object { $_ -and $_ -notmatch '^#' })
    if ($scanned.Count -gt 0) {
        $tmp = [IO.Path]::GetTempFileName()
        try {
            [IO.File]::WriteAllLines($tmp, $scanned)
            $prints = @(Invoke-Native { & ssh-keygen -lf $tmp 2>$null } | ForEach-Object { "$_" })
            foreach ($print in $prints) { Write-Host "    new host key: $print" -ForegroundColor DarkGray }
            Write-Host "    verify on the node with: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub" -ForegroundColor DarkGray
        } finally {
            Remove-Item -Path $tmp -Force -ErrorAction SilentlyContinue
        }
    }
}

<#
Explains how to grant sudo when this script cannot: both fixes need the root password, which must be
typed into the node's own prompt and never passed through here.
#>
function Show-SudoRemediation {
    param(
        [Parameter(Mandatory = $true)][string] $NodeName,
        [Parameter(Mandatory = $true)][string] $User,
        [Parameter(Mandatory = $true)][string] $HostName,
        [int]    $Port = 22,
        [switch] $InstallSudo,
        [switch] $AddGroup
    )

    if ($InstallSudo) { Write-Warn2 "sudo is not installed on $HostName" }
    if ($AddGroup)    { Write-Warn2 "$User is not in the sudo group on $HostName" }
    Write-Host '    Debian leaves both out when a root password is set during installation.' -ForegroundColor Yellow
    Write-Host '    Fix it on the node - "su -" asks for the ROOT password, type it into that prompt:' -ForegroundColor Yellow
    Write-Host ''
    $target = if ($Port -eq 22) { "$User@$HostName" } else { "-p $Port $User@$HostName" }
    Write-Host "      ssh $target" -ForegroundColor White
    Write-Host '      su -' -ForegroundColor White
    if ($InstallSudo) { Write-Host '      apt-get update && apt-get install -y sudo' -ForegroundColor White }
    if ($AddGroup)    { Write-Host "      /usr/sbin/usermod -aG sudo $User" -ForegroundColor White }
    Write-Host '      exit' -ForegroundColor White
    Write-Host '      exit' -ForegroundColor White
    Write-Host ''
    Write-Host "    Then re-run:  .\scripts\bootstrap.ps1 -Nodes $NodeName" -ForegroundColor Yellow
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

# Debian omits sudo entirely when a root password is set during installation, and then never adds the
# first user to the sudo group. Both are unfixable from here: granting them needs the root password.
$sudoCapabilityScript = @"
if command -v sudo >/dev/null 2>&1; then echo "sudo=present"; else echo "sudo=missing"; fi
if id -nG | tr ' ' '\n' | grep -qx sudo; then echo "group=present"; else echo "group=missing"; fi
"@

$b64AuthKeys   = ConvertTo-Base64Script $authorizedKeysScript
$b64Sudoers    = ConvertTo-Base64Script $sudoersScript
$b64Verify     = ConvertTo-Base64Script $verifyScript
$b64SudoCheck  = ConvertTo-Base64Script $sudoCapabilityScript

$sshCommon = @('-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=10')

$summary = @()
$sudoRemediationShown = $false

foreach ($node in $targets) {
    $nodeUser = if ($node.user) { $node.user } else { $defaultUser }
    $nodePort = if ($node.port) { [int]$node.port } else { $defaultPort }
    $dest = "$nodeUser@$($node.host)"
    $state = [ordered]@{ node = $node.name; host = $node.host; keyAuth = $false; nopasswdSudo = $false }

    Write-Host ''
    Write-Step "$($node.name)  ($dest`:$nodePort)"

    if ($ResetHostKey) {
        Write-Warn2 'forgetting the recorded host key - only correct if this node was reinstalled'
        Reset-KnownHostKey -HostName $node.host -Port $nodePort
    }

    # 1. Key-based auth --------------------------------------------------------
    $keyProbe = @('-p', $nodePort, '-i', $keyFile, '-o', 'BatchMode=yes', '-o', 'PreferredAuthentications=publickey', $dest, 'true')
    $probe = Invoke-Ssh ($sshCommon + $keyProbe)
    $keyWorks = $probe.ExitCode -eq 0

    # accept-new admits an unknown node but refuses a changed key, which is what a reimage looks like.
    if (-not $keyWorks -and (($probe.Stderr + $probe.Output) -join "`n") -match 'REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed') {
        Write-Warn2 "host key for $($node.host) does not match the recorded one"
        Write-Host '    If this node was reinstalled, re-run with -ResetHostKey. Otherwise stop: the traffic may be intercepted.' -ForegroundColor Yellow
        $summary += [pscustomobject]$state
        continue
    }

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
        $capability = Invoke-Ssh ($sshCommon + @('-p', $nodePort, '-i', $keyFile, '-o', 'BatchMode=yes', $dest, "echo $b64SudoCheck | base64 -d | bash -s"))
        $sudoMissing = $capability.Output -contains 'sudo=missing'
        $groupMissing = $capability.Output -contains 'group=missing'

        if ($sudoMissing -or $groupMissing) {
            Show-SudoRemediation -NodeName $node.name -User $nodeUser -HostName $node.host -Port $nodePort `
                -InstallSudo:$sudoMissing -AddGroup:$groupMissing
            $sudoRemediationShown = $true
        } else {
            Write-Host "    Configuring NOPASSWD sudo - you will be prompted for the sudo password on $($node.host)." -ForegroundColor Yellow
            Invoke-Native { & ssh @sshCommon -tt -p $nodePort -i $keyFile $dest "echo $b64Sudoers | base64 -d | sudo -p 'sudo password for $($node.host): ' bash -s -- $nodeUser" }
            if ((Invoke-Ssh ($sshCommon + $sudoProbe)).ExitCode -eq 0) { Write-Ok 'passwordless sudo verified'; $state.nopasswdSudo = $true }
            else { Write-Warn2 'passwordless sudo still not working' }
        }
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
    if (-not $sudoRemediationShown) {
        Write-Host '    Re-run this script, or fix manually with:  ssh <user>@<host>  then  sudo visudo -f /etc/sudoers.d/99-vanta'
    }
} else {
    Write-Host ''
    Write-Host 'All nodes ready. Next:' -ForegroundColor Green
    Write-Host '  powershell -ExecutionPolicy Bypass -File scripts/prepare-nodes.ps1'
    Write-Host '  npm install; npm run build'
    Write-Host '  Then reload the MCP server in VS Code (Command Palette > MCP: List Servers > vanta > Restart).'
}
