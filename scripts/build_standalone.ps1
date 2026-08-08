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
$SevenZipVersion = "26.02"
$SevenZipBootstrapUrl = "https://github.com/ip7z/7zip/releases/download/26.02/7zr.exe"
$SevenZipBootstrapSha256 = "56b8cc9f4971cef253644fafe54063ed7fdca551d4dee0f8c6baa81b855acd72"
$SevenZipInstallerUrl = "https://github.com/ip7z/7zip/releases/download/26.02/7z2602-x64.exe"
$SevenZipInstallerSha256 = "6745fa76dc2ea031596d8678f6f6b99c3c1b435b4164a63485adbbc7b8d82ef0"
$SevenZipRuntimeHashes = @{
    "7z.exe" = "83967f1b02b43c4efeda302795722c809e0e81b8307de73558d10484d5676a7d"
    "7z.dll" = "69fd4df057985c40e510e2fac182881c7f85e90aa13ec703f763a8fdb2ce61f8"
}

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

function Assert-UnderDirectory([string]$PathValue, [string]$RootValue, [string]$Label) {
    $full = [System.IO.Path]::GetFullPath($PathValue)
    $root = [System.IO.Path]::GetFullPath($RootValue).TrimEnd('\') + '\'
    if (-not $full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label escaped its expected root: $full"
    }
}

function Assert-FileHash([string]$PathValue, [string]$ExpectedSha256, [string]$Label) {
    if (-not (Test-Path -LiteralPath $PathValue -PathType Leaf)) {
        throw "$Label not found: $PathValue"
    }
    $actual = (Get-FileHash -LiteralPath $PathValue -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "$Label SHA-256 mismatch. Expected $ExpectedSha256, got $actual"
    }
}

function Get-VerifiedDownload(
    [string]$Url,
    [string]$Destination,
    [string]$ExpectedSha256,
    [string]$Label
) {
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -eq $ExpectedSha256.ToLowerInvariant()) {
            return
        }
        Remove-Item -LiteralPath $Destination -Force
    }

    Write-Host "Downloading verified $Label from the official 7-Zip release..."
    Invoke-WebRequest -Uri $Url -OutFile $Destination
    Assert-FileHash $Destination $ExpectedSha256 $Label
}

function Prepare-SevenZipRuntime() {
    $cacheRoot = Join-Path $NuitkaCacheRoot ("third-party\7zip-" + $SevenZipVersion)
    $bootstrap = Join-Path $cacheRoot "7zr.exe"
    $installer = Join-Path $cacheRoot "7zip-x64-installer.exe"
    $extractRoot = Join-Path $cacheRoot "runtime"
    New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null

    Get-VerifiedDownload `
        $SevenZipBootstrapUrl `
        $bootstrap `
        $SevenZipBootstrapSha256 `
        "7-Zip bootstrap executable"
    Get-VerifiedDownload `
        $SevenZipInstallerUrl `
        $installer `
        $SevenZipInstallerSha256 `
        "7-Zip x64 installer"

    $runtimeReady = $true
    foreach ($entry in $SevenZipRuntimeHashes.GetEnumerator()) {
        $runtimeFile = Join-Path $extractRoot $entry.Key
        if (-not (Test-Path -LiteralPath $runtimeFile -PathType Leaf)) {
            $runtimeReady = $false
            break
        }
        $runtimeHash = (Get-FileHash -LiteralPath $runtimeFile -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($runtimeHash -ne $entry.Value.ToLowerInvariant()) {
            $runtimeReady = $false
            break
        }
    }
    if ($runtimeReady) {
        foreach ($requiredFile in @("License.txt", "readme.txt")) {
            if (-not (Test-Path -LiteralPath (Join-Path $extractRoot $requiredFile) -PathType Leaf)) {
                $runtimeReady = $false
                break
            }
        }
    }

    if (-not $runtimeReady) {
        Assert-UnderDirectory $extractRoot $cacheRoot "7-Zip extraction directory"
        Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
        Write-Host "Extracting verified 7-Zip runtime..."
        & $bootstrap x $installer ("-o" + $extractRoot) -y | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Could not extract the verified 7-Zip runtime."
        }
    }

    foreach ($entry in $SevenZipRuntimeHashes.GetEnumerator()) {
        Assert-FileHash `
            (Join-Path $extractRoot $entry.Key) `
            $entry.Value `
            ("7-Zip runtime " + $entry.Key)
    }
    foreach ($requiredFile in @("License.txt", "readme.txt")) {
        if (-not (Test-Path -LiteralPath (Join-Path $extractRoot $requiredFile) -PathType Leaf)) {
            throw "7-Zip redistribution file not found: $requiredFile"
        }
    }
    return $extractRoot
}

Assert-UnderProject $FinalDir "OutputRoot"

$manifest = Join-Path $ReleaseSource "manifest.json"
$branding = Join-Path $ReleaseSource "branding.json"
$icon = Join-Path $ReleaseSource "icon.ico"
$remoteConfig = Join-Path $ReleaseSource "remote_manifest.json"
$releaseId = Split-Path -Leaf $ReleaseSource
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
    throw "Release manifest not found: $manifest"
}

$env:PYTHONPATH = "$ProjectRoot\src;$RepoRoot\Modlist Translate Tool\src"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
$env:NUITKA_CACHE_DIR = $NuitkaCacheRoot
$AppVersion = (& python -B -c "from modlist_translation_wizard.version import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or $AppVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "Could not determine a valid application version."
}
$FileVersion = "$AppVersion.0"

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
$SevenZipRuntimeSource = Prepare-SevenZipRuntime

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
$workerEntryPoint = Join-Path $ProjectRoot "src\modlist_translation_wizard\conversion_worker_main.py"
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
        ("--product-version={0}" -f $AppVersion),
        ("--file-version={0}" -f $FileVersion)
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

$workerNuitkaArgs = @(
    "-B",
    "-m",
    "nuitka",
    "--standalone",
    "--remove-output",
    "--disable-cache=all",
    "--windows-console-mode=disable",
    "--nofollow-import-to=tkinter,customtkinter,modlist_translation_wizard.installer_gui,modlist_translate_tool.gui",
    ("--output-dir={0}" -f $BuildRoot),
    "--output-filename=CeviriWorker.exe"
)
if ($WindowsResources) {
    $workerNuitkaArgs += @(
        "--company-name=c0kadam",
        "--product-name=Ceviri Worker",
        "--file-description=Ceviri Araci Donusum Motoru",
        ("--product-version={0}" -f $AppVersion),
        ("--file-version={0}" -f $FileVersion)
    )
}
if ($AssumeYesForDownloads) {
    $workerNuitkaArgs += "--assume-yes-for-downloads"
}
$workerNuitkaArgs += $workerEntryPoint
Invoke-Checked "python" $workerNuitkaArgs

$workerDistCandidates = @(
    (Join-Path $BuildRoot "CeviriWorker.dist"),
    (Join-Path $BuildRoot "conversion_worker_main.dist")
)
$workerDist = $workerDistCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Container } | Select-Object -First 1
if (-not $workerDist) {
    throw "Nuitka worker output not found under: $BuildRoot"
}
New-Item -ItemType Directory -Path $FinalDir -Force | Out-Null
Copy-Item -Path (Join-Path $nuitkaDist "*") -Destination $FinalDir -Recurse -Force
Copy-Item -Path (Join-Path $workerDist "*") -Destination $FinalDir -Recurse -Force

$releaseOut = Join-Path $FinalDir "release"
$assetArgs = @(
    "-B",
    (Join-Path $ProjectRoot "scripts\prepare_release_assets.py"),
    "--manifest",
    $manifest,
    "--out",
    $releaseOut,
    "--expected-list-id",
    $releaseId
)
if (Test-Path -LiteralPath $branding -PathType Leaf) {
    $assetArgs += @("--branding", $branding)
}
if (Test-Path -LiteralPath $icon -PathType Leaf) {
    $assetArgs += @("--icon", $icon)
}
if (Test-Path -LiteralPath $remoteConfig -PathType Leaf) {
    $assetArgs += @("--remote-config", $remoteConfig)
}
Invoke-Checked "python" $assetArgs

$packageResourcesOut = Join-Path $FinalDir "modlist_translation_wizard\resources"
New-Item -ItemType Directory -Path $packageResourcesOut -Force | Out-Null
$packageResourcesFull = [System.IO.Path]::GetFullPath($packageResourcesOut).TrimEnd('\') + '\'
foreach ($resourceTreeName in @("releases", "manifests", "branding")) {
    $resourceTree = Join-Path $packageResourcesOut $resourceTreeName
    $resourceTreeFull = [System.IO.Path]::GetFullPath($resourceTree)
    if (-not $resourceTreeFull.StartsWith($packageResourcesFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Embedded resource cleanup escaped package resources: $resourceTreeFull"
    }
    Remove-Item -LiteralPath $resourceTree -Recurse -Force -ErrorAction SilentlyContinue
}
Copy-Item -LiteralPath (Join-Path $releaseOut "release_config.json") -Destination (Join-Path $packageResourcesOut "release_config.json") -Force
if (Test-Path -LiteralPath (Join-Path $releaseOut "remote_manifest.json") -PathType Leaf) {
    Copy-Item -LiteralPath (Join-Path $releaseOut "remote_manifest.json") -Destination (Join-Path $packageResourcesOut "remote_manifest.json") -Force
}

$sevenZipOut = Join-Path $FinalDir "tools\7zip"
New-Item -ItemType Directory -Path $sevenZipOut -Force | Out-Null
foreach ($toolFile in @("7z.exe", "7z.dll", "License.txt", "readme.txt")) {
    Copy-Item `
        -LiteralPath (Join-Path $SevenZipRuntimeSource $toolFile) `
        -Destination (Join-Path $sevenZipOut $toolFile) `
        -Force
}
Set-Content `
    -LiteralPath (Join-Path $sevenZipOut "VERSION.txt") `
    -Value ("7-Zip " + $SevenZipVersion) `
    -Encoding ascii

$releaseDocs = @(
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "AUTHORS.md",
    "THIRD_PARTY_NOTICES.md"
)
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
$workerExe = Join-Path $FinalDir "CeviriWorker.exe"
if (-not (Test-Path -LiteralPath $workerExe -PathType Leaf)) {
    throw "Conversion worker executable not found: $workerExe"
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
