$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
Set-Location -LiteralPath $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { throw "Create .venv and install requirements and pyinstaller first." }
$Version = & $Python -c "import zhihu_collector; print(zhihu_collector.__version__)"
if ($LASTEXITCODE -ne 0 -or -not $Version) { throw "Cannot read version from zhihu_collector/__init__.py." }
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$BuildDir = Join-Path $Root "build\release-v$Version-$Stamp"
$StageDir = Join-Path $BuildDir 'dist'
$OriginalPath = $env:PATH
try {
    # Do not collect unrelated DLLs from tools such as Poppler on the caller's PATH.
    $env:PATH = (Split-Path -Parent $Python) + ';' + $env:SystemRoot + '\System32;' + $env:SystemRoot
    & $Python -m PyInstaller (Join-Path $PSScriptRoot 'zhihu_collector.spec') --noconfirm --clean --workpath (Join-Path $BuildDir 'work') --distpath $StageDir
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }
} finally { $env:PATH = $OriginalPath }
$AppDir = Get-ChildItem -LiteralPath $StageDir -Directory | Select-Object -First 1
if (-not $AppDir) { throw "No application directory produced." }
$DistDir = Join-Path $Root 'dist'
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
$TargetApp = [IO.Path]::GetFullPath((Join-Path $DistDir $AppDir.Name))
$BackupApp = [IO.Path]::GetFullPath((Join-Path $DistDir "previous-$Stamp"))
$PreviousVersionFile = Join-Path $TargetApp 'VERSION.txt'
if (Test-Path -LiteralPath $PreviousVersionFile) {
    $PreviousVersion = (Get-Content -LiteralPath $PreviousVersionFile -TotalCount 1).Trim()
    $BackupApp = [IO.Path]::GetFullPath((Join-Path $DistDir "previous-v$PreviousVersion-$Stamp"))
}
$Boundary = [IO.Path]::GetFullPath($DistDir) + [IO.Path]::DirectorySeparatorChar
foreach ($Candidate in @($TargetApp, $BackupApp)) {
    if (-not $Candidate.StartsWith($Boundary, [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe destination: $Candidate" }
}
if (Test-Path -LiteralPath $TargetApp) { Move-Item -LiteralPath $TargetApp -Destination $BackupApp }
try { Copy-Item -LiteralPath $AppDir.FullName -Destination $TargetApp -Recurse }
catch { throw "Release copy failed; previous application preserved at $BackupApp. $($_.Exception.Message)" }
Set-Content -LiteralPath (Join-Path $TargetApp 'VERSION.txt') -Value $Version -Encoding ascii
Write-Host "Build complete: $TargetApp (v$Version)"
Write-Host "Previous release and build evidence preserved. Target machine requires Playwright Chromium."
