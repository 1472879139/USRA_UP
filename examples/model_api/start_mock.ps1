$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Join-Path $PSScriptRoot '../..')
python -m model_service serve --mode mock --data-dir .model-service/mock --port 18767
exit $LASTEXITCODE
