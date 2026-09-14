<#
.SYNOPSIS
    Shared helpers for the VantaMCPd setup scripts. Dot-source it: . (Join-Path $PSScriptRoot 'common.ps1')

.DESCRIPTION
    Dot-sourcing (rather than calling) matters for Invoke-Native: the function is then created in the
    calling script's scope, so its $script:ErrorActionPreference assignment affects that script.
#>

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "    OK   $Message" -ForegroundColor Green }
function Write-Warn2{ param([string]$Message) Write-Host "    !!   $Message" -ForegroundColor Yellow }
function Write-Fail { param([string]$Message) Write-Host "    FAIL $Message" -ForegroundColor Red }

function Expand-HomePath {
    param([string]$Path)
    if ($Path -like '~*') { return (Join-Path $HOME ($Path.Substring(1).TrimStart('/', '\'))) }
    return $Path
}

<#
PowerShell 5.1 turns every native-command stderr write into a NativeCommandError, which is fatal while
$ErrorActionPreference is 'Stop' - and `2>$null` does NOT prevent it. ssh, apt, npm and winget all write
routine notices there, so every native call goes through this helper.
#>
function Invoke-Native {
    param([Parameter(Mandatory = $true)][scriptblock] $Command)
    # Scriptblocks resolve variables in the scope they were defined in, so set the script-level one.
    $previous = $ErrorActionPreference
    $script:ErrorActionPreference = 'Continue'
    try { & $Command } finally { $script:ErrorActionPreference = $previous }
}

function ConvertTo-Base64Script {
    param([string]$Script)
    # Force LF line endings; Windows CRLF breaks bash heredocs and shebangs.
    $normalized = $Script -replace "`r`n", "`n"
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($normalized))
}

function Test-TcpPort {
    # More reliable than ICMP on these boards, which often have ping disabled or filtered.
    param([string]$ComputerName, [int]$Port = 22, [int]$TimeoutMs = 2000)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($ComputerName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}
