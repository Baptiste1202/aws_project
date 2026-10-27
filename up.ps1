# Construit (ou met à jour) toute l'infrastructure : .\up.ps1
$ErrorActionPreference = "Stop"
$infra = Join-Path $PSScriptRoot "infra"
$start = Get-Date

terraform -chdir="$infra" init -input=false
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
terraform -chdir="$infra" apply -input=false -auto-approve
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host ("Infrastructure prete en {0:mm\:ss}" -f ((Get-Date) - $start)) -ForegroundColor Green
terraform -chdir="$infra" output
