[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $ConfigPath,
    [switch] $Configure,
    [switch] $Verify,
    [string[]] $SourceIds
)

$ErrorActionPreference = 'Stop'
$SourceIds = @($SourceIds | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
$credentialDirectory = Join-Path $env:LOCALAPPDATA 'VantaMCPd\sources'

function Assert-RelativePath {
    param([string] $Value)
    if ($Value -eq '') { return }
    if ($Value.Length -gt 1024) { throw 'Invalid relative path.' }
    foreach ($part in $Value.Split('/')) {
        if (-not $part -or $part -eq '.' -or $part -eq '..' -or $part -match '[\\:\x00-\x1f\x7f<>"|?*]' -or
            $part -match '[. ]$' -or $part -match '^(?i:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)') {
            throw 'Invalid relative path.'
        }
    }
}

function Get-Source {
    param([string] $Id)
    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    $matches = @($config.sources | Where-Object { $_.id -eq $Id })
    if ($matches.Count -ne 1) { throw 'Source is unknown.' }
    $source = $matches[0]
    if ($source.enabled -eq $false -or $source.type -ne 'smb' -or
        $source.server -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$' -or
        $source.share -notmatch '^[A-Za-z0-9][A-Za-z0-9._ -]{0,79}$' -or
        $source.credentialRef -notmatch '^[a-z0-9][a-z0-9-]{0,63}$') { throw 'Source configuration is invalid or disabled.' }
    Assert-RelativePath ([string]$source.root)
    return $source
}

function Connect-Source {
    param($Source)
    $tcp = New-Object Net.Sockets.TcpClient
    try {
        $pending = $tcp.BeginConnect($Source.server, 445, $null, $null)
        try {
            if (-not $pending.AsyncWaitHandle.WaitOne(5000)) { throw 'TCP_UNREACHABLE' }
            $tcp.EndConnect($pending)
        } finally { $pending.AsyncWaitHandle.Close() }
    } catch { throw 'TCP_UNREACHABLE' } finally { $tcp.Dispose() }
    $credentialFile = Join-Path $credentialDirectory ($Source.credentialRef + '.credential.xml')
    if (-not (Test-Path -LiteralPath $credentialFile)) { throw 'CREDENTIAL_MISSING' }
    $credential = Import-Clixml -LiteralPath $credentialFile
    if ($credential -isnot [System.Management.Automation.PSCredential]) { throw 'Invalid credential record.' }
    $remote = '\\' + $Source.server + '\' + $Source.share
    New-SmbMapping -RemotePath $remote -UserName $credential.UserName -Password $credential.GetNetworkCredential().Password `
        -RequirePrivacy $true -Persistent $false -ErrorAction Stop | Out-Null
    return $remote
}

function Resolve-SourcePath {
    param([string] $Remote, [string] $Root, [string] $Relative)
    Assert-RelativePath $Root
    Assert-RelativePath $Relative
    $current = $Remote
    $parts = @()
    if ($Root) { $parts += $Root.Split('/') }
    if ($Relative) { $parts += $Relative.Split('/') }
    $item = Get-Item -LiteralPath $current -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse points are not permitted.' }
    foreach ($part in $parts) {
        $current = Join-Path $current $part
        $item = Get-Item -LiteralPath $current -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse points are not permitted.' }
    }
    return $item
}

function Get-Metadata {
    param($Item, [string] $Relative)
    $size = $null
    if (-not $Item.PSIsContainer) { $size = $Item.Length }
    return @{ name = $Item.Name; path = $Relative; directory = [bool]$Item.PSIsContainer; bytes = $size;
        modifiedAt = $Item.LastWriteTimeUtc.ToString('o') }
}

function Test-SourceWrite {
    param($Directory)
    if (-not $Directory.PSIsContainer) { throw 'Write probe requires a directory.' }
    $temporary = Join-Path $Directory.FullName ('.vanta-probe-' + [Guid]::NewGuid().ToString('N'))
    $renamed = $temporary + '.done'
    $owned = $false
    try {
        $stream = New-Object IO.FileStream($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $owned = $true
        try { $stream.WriteByte(86); $stream.Flush($true) } finally { $stream.Dispose() }
        [IO.File]::Move($temporary, $renamed)
        $temporary = $renamed
        [IO.File]::Delete($temporary)
        $owned = $false
        return @{ writable = $true }
    } finally {
        if ($owned) { [IO.File]::Delete($temporary) }
    }
}

function Publish-SourceFile {
    param($Source, [string] $Remote, $Request)
    if ($Source.access -ne 'read-write') { throw 'SOURCE_READ_ONLY' }
    $relative = [string]$Request.path
    Assert-RelativePath $relative
    if (-not $relative) { throw 'Export requires a filename.' }
    $separator = $relative.LastIndexOf('/')
    $parentPath = if ($separator -ge 0) { $relative.Substring(0, $separator) } else { '' }
    $name = if ($separator -ge 0) { $relative.Substring($separator + 1) } else { $relative }
    $parent = Resolve-SourcePath $Remote ([string]$Source.root) $parentPath
    if (-not $parent.PSIsContainer) { throw 'Destination parent must be a directory.' }
    $destination = Join-Path $parent.FullName $name
    if (Test-Path -LiteralPath $destination) { throw 'DESTINATION_EXISTS' }
    $maximumMb = if ($Source.maxImportMb) { [int]$Source.maxImportMb } else { 128 }
    if ($maximumMb -lt 1 -or $maximumMb -gt 512) { throw 'Invalid transfer limit.' }
    $reader = $null
    $writer = $null
    $temporary = Join-Path $parent.FullName ('.vanta-export-' + [Guid]::NewGuid().ToString('N'))
    $owned = $false
    try {
        $reader = New-Object IO.FileStream([string]$Request.stagePath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        if ($reader.Length -gt [long]$maximumMb * 1MB -or $reader.Length -ne [long]$Request.bytes -or
            [string]$Request.sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Invalid export size or checksum.' }
        $writer = New-Object IO.FileStream($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $owned = $true
        $reader.CopyTo($writer, 524288)
        $writer.Flush($true)
        $writer.Dispose(); $writer = $null
        if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant() -ne [string]$Request.sha256) {
            throw 'Export checksum mismatch.'
        }
        $null = Resolve-SourcePath $Remote ([string]$Source.root) $parentPath
        [IO.File]::Move($temporary, $destination)
        $owned = $false
        return @{ sourceId = $Source.id; path = $relative; bytes = $reader.Length; sha256 = [string]$Request.sha256; exported = $true }
    } finally {
        if ($writer) { $writer.Dispose() }
        if ($reader) { $reader.Dispose() }
        if ($owned) { [IO.File]::Delete($temporary) }
    }
}

function Invoke-SourceRequest {
    param($Request)
    $source = Get-Source ([string]$Request.sourceId)
    $remote = Connect-Source $source
    $relative = [string]$Request.path
    if ($Request.operation -eq 'export') { return Publish-SourceFile $source $remote $Request }
    $item = Resolve-SourcePath $remote ([string]$source.root) $relative
    if ($Request.operation -eq 'check-write') {
        if ($source.access -ne 'read-write') { throw 'SOURCE_READ_ONLY' }
        return Test-SourceWrite $item
    }
    if ($Request.operation -eq 'check') {
        if (-not $item.PSIsContainer) { throw 'Source root must be a directory.' }
        $enumerator = [IO.Directory]::EnumerateFileSystemEntries($item.FullName).GetEnumerator()
        try { $null = $enumerator.MoveNext() } finally { $enumerator.Dispose() }
        return @{ accessible = $true }
    }
    if ($Request.operation -eq 'stat') { return Get-Metadata $item $relative }
    if ($Request.operation -eq 'stage') {
        if ($item.PSIsContainer) { throw 'Import requires a regular file.' }
        $maximumMb = if ($source.maxImportMb) { [int]$source.maxImportMb } else { 128 }
        if ($maximumMb -lt 1 -or $maximumMb -gt 512) { throw 'Invalid source limit.' }
        $maximum = [long]$maximumMb * 1MB
        if ($item.Length -gt $maximum) { throw 'Source file exceeds the import limit.' }
        $reader = $null
        $writer = $null
        $digest = [Security.Cryptography.SHA256]::Create()
        try {
            $reader = New-Object IO.FileStream($item.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            if ($reader.Length -gt $maximum) { throw 'Source file exceeds the import limit.' }
            $writer = New-Object IO.FileStream([string]$Request.stagePath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            $buffer = New-Object byte[] 524288
            $total = 0L
            while (($count = $reader.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $total += $count
                if ($total -gt $maximum) { throw 'Source file exceeds the import limit.' }
                $writer.Write($buffer, 0, $count)
                $null = $digest.TransformBlock($buffer, 0, $count, $buffer, 0)
            }
            $after = Resolve-SourcePath $remote ([string]$source.root) $relative
            if ($total -ne $item.Length -or $after.Length -ne $item.Length -or $after.LastWriteTimeUtc -ne $item.LastWriteTimeUtc) {
                throw 'Source file changed during import.'
            }
            $null = $digest.TransformFinalBlock([byte[]]@(), 0, 0)
            $metadata = Get-Metadata $after $relative
            $metadata.sha256 = [BitConverter]::ToString($digest.Hash).Replace('-', '').ToLowerInvariant()
            return $metadata
        } finally {
            $digest.Dispose()
            if ($writer) { $writer.Dispose() }
            if ($reader) { $reader.Dispose() }
        }
    }
    if ($Request.operation -notin @('browse', 'search', 'scan')) { throw 'Unknown operation.' }
    if (-not $item.PSIsContainer) { throw 'Discovery requires a directory.' }
    $limit = [int]$Request.limit
    $offset = [int]$Request.offset
    $maximumEntries = if ($Request.operation -eq 'scan') { 5000 } else { 200 }
    if ($limit -lt 1 -or $limit -gt $maximumEntries -or $offset -lt 0 -or $offset -gt 5000) { throw 'Invalid discovery limits.' }
    $queue = New-Object 'System.Collections.Generic.Queue[object]'
    $queue.Enqueue(@{ item = $item; relative = $relative; depth = 0 })
    $entries = New-Object 'System.Collections.Generic.List[object]'
    $scanned = 0
    $matched = 0
    $truncated = $false
    $stop = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ($queue.Count -gt 0 -and -not $stop) {
        $directory = $queue.Dequeue()
        foreach ($entryPath in [IO.Directory]::EnumerateFileSystemEntries($directory.item.FullName)) {
            $scanned++
            if ($scanned -gt 5000 -or [DateTime]::UtcNow -gt $deadline) { $truncated = $true; $stop = $true; break }
            $entry = Get-Item -LiteralPath $entryPath -Force
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            $entryRelative = if ($directory.relative) { $directory.relative + '/' + $entry.Name } else { $entry.Name }
            try { Assert-RelativePath $entryRelative } catch { continue }
            $maximumDepth = if ($Request.operation -eq 'scan') { 32 } else { 8 }
            if ($Request.operation -eq 'scan' -and $entry.PSIsContainer -and $Request.recursive -eq $false) { continue }
            if ($Request.operation -in @('search', 'scan') -and $entry.PSIsContainer -and $directory.depth -lt $maximumDepth) {
                $queue.Enqueue(@{ item = $entry; relative = $entryRelative; depth = $directory.depth + 1 })
            } elseif ($Request.operation -in @('search', 'scan') -and $entry.PSIsContainer) { $truncated = $true }
            if ($Request.operation -eq 'scan' -and $entry.PSIsContainer) { continue }
            if ($Request.operation -eq 'search' -and $entry.Name.IndexOf([string]$Request.query, [StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
            $matched++
            if ($matched -le $offset) { continue }
            if ($entries.Count -eq $limit) { $truncated = $true; $stop = $true; break }
            $entries.Add((Get-Metadata $entry $entryRelative))
        }
    }
    $nextOffset = $null
    if ($stop -and $entries.Count -eq $limit -and $offset + $entries.Count -lt 5000) { $nextOffset = $offset + $entries.Count }
    return @{ sourceId = $source.id; entries = @($entries.ToArray()); scanned = [Math]::Min($scanned, 5000);
        truncated = $truncated; nextOffset = $nextOffset }
}

function Show-SourceGuidance {
    param([string] $Code)
    if ($Code -eq 'TCP_UNREACHABLE') {
        Write-Host '  TCP 445 is unreachable. Check server power, address, SMB service, VLAN/router rules and host outbound rules.'
        Write-Host '  On the SHARE SERVER, allow inbound TCP 445 from this MCP host IP only on the trusted network profile.'
        Write-Host '  Windows: check Windows Defender Firewall > Inbound Rules > File and Printer Sharing (SMB-In).'
        Write-Host '  Do not disable the firewall, enable SMB1, or expose SMB to the Internet.'
    } elseif ($Code -eq 'CREDENTIAL_MISSING') {
        Write-Host '  Run scripts/bootstrap.ps1 -ConfigureSources in your terminal under the Windows account running MCP.'
    } else {
        Write-Host '  Check server/share/root, stored credentials, SMB3 encryption support, and share AND filesystem read/list permissions.'
        Write-Host '  Windows error 1219: review conflicting SMB sessions to this server. Vanta never disconnects existing sessions automatically.'
        Write-Host '  Rotate credentials with scripts/bootstrap.ps1 -ConfigureSources, then run -VerifySources.'
    }
}

if ($MyInvocation.InvocationName -eq '.') { return }

if ($Configure -or $Verify) {
    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    $sources = @($config.sources | Where-Object { $_.enabled -ne $false -and (-not $SourceIds -or $SourceIds -contains $_.id) })
    if (-not $sources.Count) { Write-Host 'No enabled matching SMB sources configured.'; exit 0 }
    $failed = $false
    foreach ($record in $sources) {
        $source = Get-Source $record.id
        if ($Configure) {
            $credential = Get-Credential -Message ('SMB credentials for ' + $source.id + ' (never sent to MCP)')
            if (-not $credential) { throw 'Credential setup cancelled.' }
            [IO.Directory]::CreateDirectory($credentialDirectory) | Out-Null
            $acl = New-Object Security.AccessControl.DirectorySecurity
            $acl.SetAccessRuleProtection($true, $false)
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
            $acl.SetOwner($identity)
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
            $acl.AddAccessRule($rule)
            Set-Acl -LiteralPath $credentialDirectory -AclObject $acl
            $credential | Export-Clixml -LiteralPath (Join-Path $credentialDirectory ($source.credentialRef + '.credential.xml'))
            Write-Host ('Stored user-bound credentials for ' + $source.id)
        }
        try {
            $null = Invoke-SourceRequest @{ sourceId = $source.id; operation = 'check'; path = '' }
            Write-Host ($source.id + ': TCP 445, authentication, encrypted SMB and directory listing verified.')
        } catch {
            $failed = $true
            $code = if ($_.Exception.Message -in @('TCP_UNREACHABLE', 'CREDENTIAL_MISSING')) { $_.Exception.Message } else { 'SMB_ACCESS_FAILED' }
            Write-Host ($source.id + ': ' + $code)
            Show-SourceGuidance $code
        }
    }
    if ($failed) { exit 1 }
    exit 0
}

try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $result = Invoke-SourceRequest $request
    @{ ok = $true; result = $result } | ConvertTo-Json -Depth 12 -Compress
} catch {
    $code = if ($_.Exception.Message -in @('TCP_UNREACHABLE', 'CREDENTIAL_MISSING', 'SOURCE_READ_ONLY', 'DESTINATION_EXISTS')) { $_.Exception.Message } else { 'SMB_ACCESS_FAILED' }
    @{ ok = $false; code = $code } | ConvertTo-Json -Compress
}