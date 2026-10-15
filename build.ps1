param(
    [string]$Python = "python",
    [ValidatePattern('^[A-Za-z][A-Za-z0-9_-]*$')]
    [string]$OutputName = "GoldMonitor",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$toolRoot = Join-Path $projectRoot ".build-tools"
$packageRoot = Join-Path $toolRoot "python"
$entryPoint = Join-Path $projectRoot "gold_monitor.pyw"
$artifact = Join-Path $projectRoot ("dist\" + $OutputName + ".exe")
$buildWorkspace = Join-Path $projectRoot ("build\run-" + [Guid]::NewGuid().ToString("N"))
$previousPythonPath = $env:PYTHONPATH
$previousBuildCache = $env:PYINSTALLER_CONFIG_DIR

if (-not (Test-Path -LiteralPath $entryPoint)) {
    throw "Missing entry point: $entryPoint"
}

# Do not replace a running copy. An alternate output name can be selected explicitly.
if (Test-Path -LiteralPath $artifact) {
    $artifactHandle = $null
    try {
        $artifactHandle = [IO.File]::Open($artifact, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch {
        throw "Cannot replace $artifact. The file may be running or locked. Close it yourself, or use -OutputName GoldMonitor-Updated to build a separate executable. No application was stopped."
    } finally {
        if ($artifactHandle) { $artifactHandle.Dispose() }
    }
}

try {
    New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
    if (-not $SkipInstall) {
        & $Python -m pip install --target $packageRoot --upgrade `
            --cache-dir (Join-Path $toolRoot "pip-cache") `
            --disable-pip-version-check -r (Join-Path $projectRoot "requirements-build.txt")
        if ($LASTEXITCODE -ne 0) { throw "Unable to install the local build tools." }
    }

    $env:PYTHONPATH = $packageRoot
    if ($previousPythonPath) { $env:PYTHONPATH += [IO.Path]::PathSeparator + $previousPythonPath }
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $toolRoot "cache"

    & $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name $OutputName `
        --icon (Join-Path $projectRoot "assets\gold.ico") `
        --add-data ((Join-Path $projectRoot "assets\gold.ico") + ";assets") `
        --distpath (Join-Path $projectRoot "dist") `
        --workpath $buildWorkspace `
        --specpath (Join-Path $projectRoot "build") `
        --paths $projectRoot $entryPoint
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

    if (-not (Test-Path -LiteralPath $artifact)) { throw "Build completed without the expected executable." }
    Write-Host "Built: $artifact"
    Write-Host "Build diagnostics: $buildWorkspace"
} finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYINSTALLER_CONFIG_DIR = $previousBuildCache
}
