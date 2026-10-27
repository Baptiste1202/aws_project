# Supprime TOUTE l'infrastructure (plus aucun cout) : .\down.ps1
$ErrorActionPreference = "Stop"
$infra = Join-Path $PSScriptRoot "infra"
$start = Get-Date

terraform -chdir="$infra" init -input=false
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
terraform -chdir="$infra" destroy -input=false -auto-approve
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ("Tout est supprime ({0:mm\:ss})" -f ((Get-Date) - $start)) -ForegroundColor Green
