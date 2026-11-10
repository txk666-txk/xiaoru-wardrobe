$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'shared_fashn_service.ps1')
Stop-SharedFashnService
