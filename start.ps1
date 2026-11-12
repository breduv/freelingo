$ErrorActionPreference = 'Stop'

try {
    if (!(Test-Path -LiteralPath (Join-Path $PSScriptRoot '.env'))) {
        throw 'Copy .env.example to .env and set database/Redis passwords and SECRET_KEY first.'
    }
    & docker compose --project-directory $PSScriptRoot -f (Join-Path $PSScriptRoot 'docker-compose.yml') -f (Join-Path $PSScriptRoot 'docker-compose.local.yml') up -d --build --wait --wait-timeout 180
    if ($LASTEXITCODE -ne 0) { throw 'FreeLingo did not start. Inspect the Compose service logs.' }
    Write-Host 'FreeLingo: http://localhost:3000'
    Write-Host 'AI settings: Settings > AI settings (administrator only).'
    Write-Host 'Speech models may still be downloading. Ollama must be running if selected.'
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
