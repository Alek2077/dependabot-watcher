param([Parameter(Mandatory=$true)][string]$Helper)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$root = Join-Path ([IO.Path]::GetTempPath()) ('watcher-publish-contract-' + [Guid]::NewGuid().ToString('N'))
$published = Join-Path $root 'published'
$cache = Join-Path $root 'packages'
$package = Join-Path $cache 'microsoft.windowsappsdk.runtime/2.5.1/tools/MSIX/win10-x64'
$assets = Join-Path $root 'project.assets.json'
$name = 'Microsoft.WindowsAppRuntime.Insights.Resource.dll'
$archivePath = Join-Path $package 'Microsoft.WindowsAppRuntime.2.msix'
New-Item -ItemType Directory -Path $published,$package -Force | Out-Null
function Save-Assets([string]$Relative = 'microsoft.windowsappsdk.runtime/2.5.1') {
    $folders = @{}; $folders[$cache] = @{}
    @{ libraries = @{ 'Microsoft.WindowsAppSDK.Runtime/2.5.1' = @{type='package';path=$Relative} }; packageFolders=$folders } |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $assets
}
function Must-Reject([scriptblock]$Action) {
    $rejected = $false
    try { & $Action | Out-Null } catch { $rejected = $true }
    if (!$rejected) { throw 'Invalid publish completion was accepted.' }
}
try {
    Save-Assets
    $archive = [IO.Compression.ZipFile]::Open($archivePath,[IO.Compression.ZipArchiveMode]::Create)
    try {
        $stream = $archive.CreateEntry($name).Open()
        try { $bytes = [Text.Encoding]::UTF8.GetBytes('controlled resource fixture'); $stream.Write($bytes,0,$bytes.Length) }
        finally { $stream.Dispose() }
    } finally { $archive.Dispose() }
    & $Helper -PublishedDirectory $published -AssetsFile $assets | Out-Null
    $target = Join-Path $published $name
    if ([IO.File]::ReadAllText($target) -cne 'controlled resource fixture') { throw 'Exact resource was not extracted.' }
    & $Helper -PublishedDirectory $published -AssetsFile $assets -PackageCache $cache | Out-Null
    [IO.File]::WriteAllText($target,'different resource')
    Must-Reject { & $Helper -PublishedDirectory $published -AssetsFile $assets }
    if ([IO.File]::ReadAllText($target) -cne 'different resource') { throw 'Mismatched existing resource was overwritten.' }
    Save-Assets '../other-package'
    Must-Reject { & $Helper -PublishedDirectory $published -AssetsFile $assets }
    Save-Assets
    Must-Reject { & $Helper -PublishedDirectory $published -AssetsFile $assets -PackageCache (Join-Path $root 'absent') }
    Write-Output 'Publish completion contracts passed: exact extraction, idempotence, mismatch preservation, invalid path and absent package.'
} finally {
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
