$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'shared_fashn_service.ps1')
Start-SharedFashnService -ModelPython $env:FAY_FASHN_VTON_PYTHON -WeightsDir $env:FAY_FASHN_VTON_WEIGHTS_DIR | Out-Null
