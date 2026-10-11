param(
    [Parameter(Mandatory=$true)][string]$PublishedDirectory,
    [Parameter(Mandatory=$true)][string]$AssetsFile,
    [string]$PackageCache
)
$ErrorActionPreference = 'Stop'
# Windows App SDK 2.5.1 can omit this resource from self-contained publish.
# Use only the exact runtime package resolved by this build, never another app
# or a system DLL. The packager independently requires the completed resource.
$published = (Get-Item -LiteralPath $PublishedDirectory).FullName
$assets = Get-Content -LiteralPath $AssetsFile -Raw | ConvertFrom-Json
$runtime = @($assets.libraries.PSObject.Properties | Where-Object {
    $_.Name -match '^Microsoft\.WindowsAppSDK\.Runtime/[^/]+$'
})
if ($runtime.Count -ne 1 -or $runtime[0].Value.type -ne 'package') { throw 'Expected one resolved Windows App SDK runtime package.' }
$relative = $runtime[0].Value.path
if ($relative -notmatch '^microsoft\.windowsappsdk\.runtime/[0-9][A-Za-z0-9.+-]*$' -or $relative -ine $runtime[0].Name) { throw 'Invalid or mismatched runtime package path.' }
$folders = if ($PackageCache) { @($PackageCache) } else { @($assets.packageFolders.PSObject.Properties.Name) }
$archives = @($folders | ForEach-Object {
    $candidate = Join-Path $_ "$relative/tools/MSIX/win10-x64/Microsoft.WindowsAppRuntime.2.msix"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { (Get-Item -LiteralPath $candidate).FullName }
} | Select-Object -Unique)
if ($archives.Count -ne 1) { throw 'The exact restored runtime MSIX is missing or ambiguous.' }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($archives[0])
try {
    $name = 'Microsoft.WindowsAppRuntime.Insights.Resource.dll'
    $entries = @($archive.Entries | Where-Object { $_.FullName -ceq $name })
    if ($entries.Count -ne 1 -or $entries[0].Length -lt 1 -or $entries[0].Length -gt 16777216) { throw 'Missing, repeated or oversized runtime resource.' }
    $target = Join-Path $published $name
    if (Test-Path -LiteralPath $target) {
        # A rerun is a verification, not permission to overwrite another DLL.
        $stream = $entries[0].Open()
        $hash = [Security.Cryptography.SHA256]::Create()
        try { $expected = [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-','') }
        finally { $stream.Dispose(); $hash.Dispose() }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash -cne $expected) { throw 'Published runtime resource differs from its resolved package.' }
    } else {
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entries[0],$target,$false)
    }
} finally { $archive.Dispose() }
Write-Output 'Version-matched Windows runtime resource verified.'
