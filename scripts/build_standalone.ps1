[CmdletBinding()]
param(
    [string]$OutputRoot,
    [string]$ReleaseSource,
    [switch]$SkipTests,
    [switch]$AssumeYesForDownloads,
    [switch]$NoZip,
    [switch]$WindowsResources
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoRoot = (Resolve-Path (Join-Path $ProjectRoot "..")).Path
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $ProjectRoot "dist\standalone"
}
if (-not $ReleaseSource) {
    $ReleaseSource = Join-Path $ProjectRoot "src\modlist_translation_wizard\resources\releases\lorerim"
}
$BuildStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$TempBuildParent = Join-Path ([System.IO.Path]::GetTempPath()) "MTW-Nuitka"
$BuildRoot = Join-Path $TempBuildParent ("nuitka-" + $BuildStamp)
$NuitkaCacheRoot = Join-Path $TempBuildParent "cache"
$FinalDir = [System.IO.Path]::GetFullPath($OutputRoot)
$ReleaseSource = [System.IO.Path]::GetFullPath($ReleaseSource)

function Assert-UnderProject([string]$PathValue, [string]$Label) {
    $full = [System.IO.Path]::GetFullPath($PathValue)
    $root = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\') + '\'
    if (-not $full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay under project root for this build script: $full"
    }
}

function Invoke-Checked([string]$FilePath, [string[]]$Arguments) {
    Write-Host ">> $FilePath $($Arguments -join ' ')"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath"
    }
}

Assert-UnderProject $FinalDir "OutputRoot"

$manifest = Join-Path $ReleaseSource "manifest.json"
$branding = Join-Path $ReleaseSource "branding.json"
$icon = Join-Path $ReleaseSource "icon.ico"
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
    throw "Release manifest not found: $manifest"
}

$env:PYTHONPATH = "$ProjectRoot\src;$RepoRoot\Modlist Translate Tool\src"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
$env:NUITKA_CACHE_DIR = $NuitkaCacheRoot

if (-not $SkipTests) {
    Push-Location $ProjectRoot
    try {
        Invoke-Checked "python" @("-B", "-m", "pytest")
    }
    finally {
        Pop-Location
    }
}

Invoke-Checked "python" @("-B", "-m", "nuitka", "--version")

Remove-Item -LiteralPath $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $FinalDir) {
    Remove-Item -LiteralPath $FinalDir -Recurse -Force -ErrorAction Stop
}
if (Test-Path -LiteralPath $FinalDir) {
    throw "OutputRoot could not be removed before build: $FinalDir"
}
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $FinalDir) -Force | Out-Null

$resourcesSource = Join-Path $ProjectRoot "src\modlist_translation_wizard\resources"
$entryPoint = Join-Path $ProjectRoot "src\modlist_translation_wizard\__main__.py"
$nuitkaArgs = @(
    "-B",
    "-m",
    "nuitka",
    "--standalone",
    "--remove-output",
    "--enable-plugin=tk-inter",
    "--disable-cache=all",
    "--windows-console-mode=disable",
    "--include-package=modlist_translation_wizard",
    "--include-package=modlist_translate_tool",
    "--include-package-data=customtkinter",
    ("--include-data-dir={0}=modlist_translation_wizard/resources" -f $resourcesSource),
    "--nofollow-import-to=pytest",
    ("--output-dir={0}" -f $BuildRoot),
    "--output-filename=CeviriAraci.exe"
)
if (Test-Path -LiteralPath $icon -PathType Leaf) {
    $nuitkaArgs += ("--windows-icon-from-ico={0}" -f $icon)
}
if ($WindowsResources) {
    $nuitkaArgs += @(
        "--company-name=c0kadam",
        "--product-name=Ceviri Araci",
        "--file-description=Ceviri Araci",
        "--product-version=0.1.0",
        "--file-version=0.1.0.0"
    )
}
if ($AssumeYesForDownloads) {
    $nuitkaArgs += "--assume-yes-for-downloads"
}
$nuitkaArgs += $entryPoint
Invoke-Checked "python" $nuitkaArgs

$nuitkaDistCandidates = @(
    (Join-Path $BuildRoot "CeviriAraci.dist"),
    (Join-Path $BuildRoot "__main__.dist")
)
$nuitkaDist = $nuitkaDistCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Container } | Select-Object -First 1
if (-not $nuitkaDist) {
    throw "Nuitka output not found under: $BuildRoot"
}
New-Item -ItemType Directory -Path $FinalDir -Force | Out-Null
Copy-Item -Path (Join-Path $nuitkaDist "*") -Destination $FinalDir -Recurse -Force

$releaseOut = Join-Path $FinalDir "release"
$assetArgs = @(
    "-B",
    (Join-Path $ProjectRoot "scripts\prepare_release_assets.py"),
    "--manifest",
    $manifest,
    "--out",
    $releaseOut
)
if (Test-Path -LiteralPath $branding -PathType Leaf) {
    $assetArgs += @("--branding", $branding)
}
if (Test-Path -LiteralPath $icon -PathType Leaf) {
    $assetArgs += @("--icon", $icon)
}
Invoke-Checked "python" $assetArgs

$releaseDocs = @("README.md", "LICENSE", "SECURITY.md", "AUTHORS.md")
foreach ($docName in $releaseDocs) {
    $docPath = Join-Path $ProjectRoot $docName
    if (Test-Path -LiteralPath $docPath -PathType Leaf) {
        Copy-Item -LiteralPath $docPath -Destination (Join-Path $FinalDir $docName) -Force
    }
}

$mainExe = Join-Path $FinalDir "CeviriAraci.exe"
if (-not (Test-Path -LiteralPath $mainExe -PathType Leaf)) {
    throw "Main executable not found: $mainExe"
}

if (-not $NoZip) {
    $zipPath = "$FinalDir.zip"
    Assert-UnderProject $zipPath "ZipPath"
    Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    Compress-Archive -LiteralPath $FinalDir -DestinationPath $zipPath -CompressionLevel Optimal
    $hash = Get-FileHash -LiteralPath $zipPath -Algorithm SHA256
    Write-Host "ZIP: $zipPath"
    Write-Host "ZIP SHA-256: $($hash.Hash.ToLowerInvariant())"
}

Write-Host "Standalone folder: $FinalDir"
Write-Host "Release manifest: $(Join-Path $releaseOut 'manifest.json')"
Write-Host "Done."
