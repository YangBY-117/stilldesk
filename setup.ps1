param([switch]$Messaging, [switch]$TestDeps)
$ErrorActionPreference = 'Stop'
$setupArgs = @((Join-Path $PSScriptRoot 'scripts\setup.mjs'))
if ($Messaging) { $setupArgs += '--messaging' }
if ($TestDeps) { $setupArgs += '--test-deps' }
& node @setupArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
