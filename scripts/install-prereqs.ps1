<#
.SYNOPSIS
    Prepares this Windows machine to build and run VantaMCPd.

.DESCRIPTION
    Verifies (and, unless -Check is given, installs) everything the MCP daemon needs:
      1. Windows OpenSSH client - ssh / ssh-keygen, used by scripts/bootstrap.ps1.
      2. Node.js LTS (>= 20.11) and npm, via winget.
      3. npm dependencies and a TypeScript build.
      4. TCP reachability to every node in the local inventory, once it exists.
      5. Presence of the cluster SSH key.

    The script is idempotent: anything already in place is reported and skipped.
    Exit code is 0 when every check passes, 1 otherwise.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/install-prereqs.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/install-prereqs.ps1 -Check
#>
[CmdletBinding()]
param(
    [string] $ConfigPath,
    [switch] $Check,
    [switch] $SkipBuild,
    [switch] $SkipNetworkTest
)

$ErrorActionPreference = 'Stop'
# $PSScriptRoot is empty inside param() defaults of an advanced script on PS 5.1.
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $ConfigPath) { $ConfigPath = Join-Path $repoRoot 'cluster.config.local.json' }
$minNodeVersion = [version]'20.11.0'
. (Join-Path $PSScriptRoot 'common.ps1')

$results = New-Object System.Collections.Generic.List[object]
function Add-Result {
    param([string]$Name, [ValidateSet('ok', 'installed', 'missing', 'warn')][string]$Status, [string]$Detail)
    $results.Add([pscustomobject]@{ Check = $Name; Status = $Status; Detail = $Detail })
}

function Update-SessionPath {
    # winget writes the new PATH to the registry; the running process needs it re-read.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Get-NativeOutput {
    # ssh -V writes its banner to stderr; letting PowerShell merge that stream yields a NativeCommandError,
    # so the redirect is done by cmd.exe and only plain text comes back.
    param([string]$CommandLine)
    return (& cmd.exe /c "$CommandLine 2>&1" | Out-String).Trim()
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

Write-Step "VantaMCPd prerequisites  (repo: $repoRoot)"
Write-Host "    PowerShell $($PSVersionTable.PSVersion)  on  $([Environment]::OSVersion.VersionString)" -ForegroundColor DarkGray
if ($Check) { Write-Host '    -Check: reporting only, nothing will be installed.' -ForegroundColor DarkGray }

# ------------------------------------------------------------------ 1. OpenSSH
Write-Step 'OpenSSH client (ssh, ssh-keygen)'
$missingSsh = @('ssh', 'ssh-keygen') | Where-Object { -not (Get-Command $_ -ErrorAction SilentlyContinue) }

if (-not $missingSsh) {
    $sshVersion = Get-NativeOutput 'ssh -V'
    Write-Ok $sshVersion
    Add-Result 'openssh-client' 'ok' $sshVersion
} elseif ($Check) {
    Write-Fail "not found: $($missingSsh -join ', ')"
    Add-Result 'openssh-client' 'missing' 'Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0'
} elseif (-not (Test-Admin)) {
    Write-Fail 'not found, and installing it needs an elevated shell.'
    Write-Host '         Run as Administrator:  Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0' -ForegroundColor Yellow
    Add-Result 'openssh-client' 'missing' 'needs elevation'
} else {
    Write-Host '    Installing the OpenSSH.Client Windows capability...'
    Add-WindowsCapability -Online -Name 'OpenSSH.Client~~~~0.0.1.0' | Out-Null
    Update-SessionPath
    if (Get-Command ssh -ErrorAction SilentlyContinue) {
        Write-Ok 'OpenSSH client installed'
        Add-Result 'openssh-client' 'installed' 'OpenSSH.Client capability'
    } else {
        Write-Fail 'OpenSSH client still not on PATH - reopen the terminal and re-run.'
        Add-Result 'openssh-client' 'missing' 'reopen terminal'
    }
}

# ------------------------------------------------------------------- 2. Node.js
Write-Step "Node.js >= $minNodeVersion and npm"

function Get-NodeVersion {
    $cmd = Get-Command node -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    $raw = Get-NativeOutput 'node -v'
    if ($raw -match 'v(\d+\.\d+\.\d+)') { return [version]$matches[1] }
    return $null
}

$nodeVersion = Get-NodeVersion
if (-not $nodeVersion) { Update-SessionPath; $nodeVersion = Get-NodeVersion }

if ($nodeVersion -and $nodeVersion -ge $minNodeVersion) {
    Write-Ok "node v$nodeVersion  ($((Get-Command node).Source))"
    Add-Result 'node' 'ok' "v$nodeVersion"
} elseif ($Check) {
    $detail = if ($nodeVersion) { "v$nodeVersion is older than $minNodeVersion" } else { 'not installed' }
    Write-Fail $detail
    Add-Result 'node' 'missing' $detail
} else {
    if ($nodeVersion) { Write-Warn2 "v$nodeVersion is older than $minNodeVersion - upgrading" } else { Write-Warn2 'not installed' }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Fail 'winget is unavailable. Install Node.js LTS manually from https://nodejs.org/en/download'
        Add-Result 'node' 'missing' 'winget unavailable'
    } else {
        Write-Host '    winget install OpenJS.NodeJS.LTS ...'
        Invoke-Native {
            & winget install --id OpenJS.NodeJS.LTS --exact --source winget `
                --accept-package-agreements --accept-source-agreements --disable-interactivity
        }
        $wingetExit = $LASTEXITCODE
        Update-SessionPath
        $nodeVersion = Get-NodeVersion
        if ($nodeVersion -and $nodeVersion -ge $minNodeVersion) {
            Write-Ok "node v$nodeVersion installed"
            Add-Result 'node' 'installed' "v$nodeVersion"
        } else {
            Write-Fail "winget exited with $wingetExit and node is still unusable. Close and reopen the terminal, then re-run this script."
            Add-Result 'node' 'missing' "winget exit $wingetExit"
        }
    }
}

$npmCmd = Get-Command npm -ErrorAction SilentlyContinue
if ($npmCmd) {
    $npmVersion = Get-NativeOutput 'npm -v'
    Write-Ok "npm $npmVersion"
    Add-Result 'npm' 'ok' $npmVersion
} else {
    Write-Fail 'npm not found (it ships with Node.js).'
    Add-Result 'npm' 'missing' 'ships with Node.js'
}

# --------------------------------------------------------- 3. Install and build
Write-Step 'Dependencies and build'
if ($Check -or $SkipBuild) {
    $hasModules = Test-Path (Join-Path $repoRoot 'node_modules')
    $hasDist = Test-Path (Join-Path $repoRoot 'dist\index.js')
    if ($hasModules) { Write-Ok 'node_modules present' } else { Write-Warn2 'node_modules missing - run npm install' }
    if ($hasDist) { Write-Ok 'dist/index.js present' } else { Write-Warn2 'dist/index.js missing - run npm run build' }
    Add-Result 'build' $(if ($hasModules -and $hasDist) { 'ok' } else { 'warn' }) "node_modules=$hasModules dist=$hasDist"
} elseif (-not $npmCmd) {
    Write-Warn2 'skipped - npm unavailable'
    Add-Result 'build' 'missing' 'npm unavailable'
} else {
    Push-Location $repoRoot
    try {
        Write-Host '    npm install ...'
        Invoke-Native { & npm install }
        if ($LASTEXITCODE -ne 0) { throw "npm install failed with exit code $LASTEXITCODE" }
        Write-Host '    npm run build ...'
        Invoke-Native { & npm run build }
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed with exit code $LASTEXITCODE" }
        Write-Ok 'dist/index.js built'
        Add-Result 'build' 'installed' 'npm install + build'
    } catch {
        Write-Fail $_.Exception.Message
        Add-Result 'build' 'missing' $_.Exception.Message
    } finally {
        Pop-Location
    }
}

# ------------------------------------------------------------- 4. Cluster reach
Write-Step 'Cluster reachability'
if ($SkipNetworkTest) {
    Write-Warn2 'skipped (-SkipNetworkTest)'
} elseif (-not (Test-Path $ConfigPath)) {
    Write-Warn2 "no local inventory yet ($ConfigPath)"
    Write-Host '         scripts/bootstrap.ps1 will create one from cluster.config.example.json.' -ForegroundColor DarkGray
    Add-Result 'inventory' 'warn' 'run scripts/bootstrap.ps1'
} else {
    $config = Get-Content -Raw -Path $ConfigPath | ConvertFrom-Json
    $defaultPort = if ($config.defaults.port) { [int]$config.defaults.port } else { 22 }
    $unreachable = @()
    foreach ($node in $config.nodes) {
        $port = if ($node.port) { [int]$node.port } else { $defaultPort }
        if (Test-TcpPort -ComputerName $node.host -Port $port) {
            Write-Ok "$($node.name)  $($node.host):$port"
        } else {
            Write-Fail "$($node.name)  $($node.host):$port  no answer"
            $unreachable += $node.name
        }
    }
    if ($unreachable) { Add-Result 'cluster-reachable' 'warn' "unreachable: $($unreachable -join ', ')" }
    else { Add-Result 'cluster-reachable' 'ok' "$($config.nodes.Count) node(s)" }

    $keyFile = Expand-HomePath $(if ($config.defaults.privateKeyPath) { $config.defaults.privateKeyPath } else { '~/.ssh/vanta_cluster_ed25519' })
    if (Test-Path $keyFile) {
        Write-Ok "cluster SSH key: $keyFile"
        Add-Result 'ssh-key' 'ok' $keyFile
    } else {
        Write-Warn2 "cluster SSH key missing - run scripts/bootstrap.ps1 to create and deploy it"
        Add-Result 'ssh-key' 'warn' "missing: $keyFile"
    }
}

# ----------------------------------------------------------------- 5. Summary
Write-Host ''
Write-Step 'Summary'
$results | Format-Table -AutoSize

$failed = @($results | Where-Object { $_.Status -eq 'missing' })
if ($failed) {
    Write-Host "$($failed.Count) prerequisite(s) still missing." -ForegroundColor Red
    exit 1
}

Write-Host 'Next: scripts/bootstrap.ps1  (create the local inventory, deploy the SSH key + sudo rule), then scripts/prepare-nodes.ps1.' -ForegroundColor Cyan
exit 0
