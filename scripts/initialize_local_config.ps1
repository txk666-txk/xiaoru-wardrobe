param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$templates = @(
    @{ Source = "system.conf.example"; Target = "system.conf" },
    @{ Source = "config.example.json"; Target = "config.json" },
    @{ Source = "qa.example.csv"; Target = "qa.csv" },
    @{ Source = "faymcp/data/mcp_servers.example.json"; Target = "faymcp/data/mcp_servers.json" },
    @{ Source = "faymcp/data/mcp_prestart_tools.example.json"; Target = "faymcp/data/mcp_prestart_tools.json" }
)

foreach ($template in $templates) {
    $source = Join-Path $projectRoot $template.Source
    $target = Join-Path $projectRoot $template.Target

    if (-not (Test-Path -LiteralPath $source)) {
        throw "Missing template: $source"
    }

    if ((Test-Path -LiteralPath $target) -and -not $Force) {
        Write-Host "[keep] $($template.Target)"
        continue
    }

    Copy-Item -LiteralPath $source -Destination $target -Force
    Write-Host "[created] $($template.Target)"
}

Write-Host "Local configuration is ready. Fill credentials only in system.conf."
