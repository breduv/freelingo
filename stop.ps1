$ErrorActionPreference = 'Stop'

& docker compose --project-directory $PSScriptRoot -f (Join-Path $PSScriptRoot 'docker-compose.yml') -f (Join-Path $PSScriptRoot 'docker-compose.local.yml') stop --timeout 30
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host 'FreeLingo stopped. Saved data and settings are preserved.'
