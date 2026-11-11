[CmdletBinding()]
param(
    [string]$MsysRoot = $(if ($env:MSYS2_ROOT) { $env:MSYS2_ROOT } else { 'C:\msys64' }),
    [string]$BuildDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'build'),
    [ValidateRange(1, 64)][int]$Jobs = 4,
    [ValidateSet('debug', 'debugoptimized', 'release', 'minsize', 'plain')]
    [string]$BuildType = 'release',
    [switch]$Reconfigure
)
$ErrorActionPreference = 'Stop'
$sourceDir = Split-Path -Parent $PSScriptRoot
$bash = Join-Path $MsysRoot 'usr\bin\bash.exe'
if (-not (Test-Path -LiteralPath $bash -PathType Leaf)) {
    throw "MSYS2 bash was not found at $bash. Pass -MsysRoot with the MSYS2 installation directory."
}
$absoluteBuildDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($BuildDir)
$settings = @{
    MSYSTEM = 'MINGW64'
    CHERE_INVOKING = '1'
    MSYS2_PATH_TYPE = 'minimal'
    DM_MC02_SOURCE_WIN = $sourceDir
    DM_MC02_BUILD_WIN = $absoluteBuildDir
    QEMU_BUILD_JOBS = "$Jobs"
    QEMU_BUILD_TYPE = $BuildType
    QEMU_RECONFIGURE = $(if ($Reconfigure) { '1' } else { '0' })
}
$previous = @{}
try {
    foreach ($name in $settings.Keys) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $settings[$name], 'Process')
    }
    & $bash -lc 'exec bash "$(cygpath -u "$DM_MC02_SOURCE_WIN")/scripts/build-dm-mc02.sh" "$(cygpath -u "$DM_MC02_BUILD_WIN")"'
    if ($LASTEXITCODE -ne 0) { throw "QEMU build failed with exit code $LASTEXITCODE." }
}
finally {
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
}
