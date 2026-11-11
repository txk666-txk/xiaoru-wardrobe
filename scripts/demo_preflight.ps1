param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$errors = [System.Collections.Generic.List[string]]::new()
$warnings = [System.Collections.Generic.List[string]]::new()

function Read-IniValue {
    param([string[]]$Lines, [string]$Name)
    $pattern = "^\s*" + [regex]::Escape($Name) + "\s*="
    $line = $Lines | Where-Object { $_ -match $pattern } | Select-Object -Last 1
    if (-not $line) { return "" }
    return (($line -split "=", 2)[1]).Trim()
}

function Require-Values {
    param([string[]]$Lines, [string[]]$Names, [string]$Label)
    $missing = @($Names | Where-Object { -not (Read-IniValue $Lines $_) })
    if ($missing.Count -gt 0) {
        $errors.Add("$Label missing configuration: $($missing -join ', ')")
    }
}

$pythonCandidates = @(
    (Join-Path $projectRoot ".venv/python.exe"),
    (Join-Path $projectRoot ".venv/Scripts/python.exe")
)
if (-not $PythonExe) {
    $PythonExe = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if ($PythonExe -and -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable does not exist: $PythonExe"
}
if (-not $pythonExe) {
    $errors.Add("Python runtime was not found under .venv. Use -PythonExe to select an existing environment.")
} else {
    $PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
}

$requiredFiles = @("system.conf", "config.json", "qa.csv")
foreach ($file in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $file))) {
        $errors.Add("Missing local file: $file")
    }
}

$systemPath = Join-Path $projectRoot "system.conf"
$systemLines = if (Test-Path -LiteralPath $systemPath) { Get-Content -LiteralPath $systemPath } else { @() }
$asrMode = Read-IniValue $systemLines "ASR_mode"
$ttsMode = Read-IniValue $systemLines "tts_module"
$startMode = Read-IniValue $systemLines "start_mode"

if ($asrMode -eq "ali") {
    Require-Values $systemLines @("ali_nls_key_id", "ali_nls_key_secret", "ali_nls_app_key") "Alibaba ASR"
}
if ($ttsMode -eq "ali") {
    Require-Values $systemLines @("ali_tss_key_id", "ali_tss_key_secret", "ali_tss_app_key") "Alibaba TTS"
}
Require-Values $systemLines @("gpt_api_key", "gpt_base_url", "gpt_model_engine") "LLM"

if ($startMode -notin @("web", "common")) {
    $errors.Add("Unsupported start_mode: $startMode")
}

$configPath = Join-Path $projectRoot "config.json"
$mcpPath = Join-Path $projectRoot "faymcp/data/mcp_servers.json"
if (Test-Path -LiteralPath $configPath) {
    try {
        if (-not $pythonExe) {
            throw "Python runtime is unavailable for JSON validation"
        }
        & $pythonExe (Join-Path $PSScriptRoot "validate_json.py") $configPath
        if ($LASTEXITCODE -ne 0) {
            throw "JSON validation failed"
        }
        if (-not (Test-Path -LiteralPath $mcpPath)) {
            throw "MCP server configuration is missing"
        }
        & $pythonExe (Join-Path $PSScriptRoot "validate_json.py") $mcpPath
        if ($LASTEXITCODE -ne 0) {
            throw "JSON validation failed"
        }
        $preflightValues = @(& $pythonExe (Join-Path $PSScriptRoot "inspect_preflight_json.py") $configPath $mcpPath)
        if ($LASTEXITCODE -ne 0) {
            throw "Could not inspect JSON configuration"
        }
        $preflight = @{}
        foreach ($line in $preflightValues) {
            $parts = $line -split "=", 2
            if ($parts.Count -eq 2) { $preflight[$parts[0]] = $parts[1] }
        }
        if ($preflight["config_record_enabled"] -ne "true") {
            $warnings.Add("Microphone input is disabled in config.json; web/text chat remains available")
        }
        if ($preflight["config_play_sound"] -ne "true") {
            $warnings.Add("Local audio playback is disabled")
        }
        if ($preflight["config_automatic_player_status"] -eq "true") {
            $warnings.Add("Automatic player is enabled and may duplicate browser audio")
        }
        Write-Host "MCP autostart servers: $($preflight['mcp_autostart_count'])"
    }
    catch {
        $errors.Add("Configuration check failed: $($_.Exception.Message)")
    }
}

if ($pythonExe) {
    $desktopModules = if ($startMode -eq "common") { @("PyQt5", "PyQt5.QtWebEngineWidgets") } else { @() }
    $moduleList = @("flask", "requests", "websockets", "pyaudio", "mcp", "langgraph.graph", "langsmith", "chromadb", "sentence_transformers") + $desktopModules
    & $pythonExe (Join-Path $PSScriptRoot "probe_python_modules.py") @moduleList
    if ($LASTEXITCODE -ne 0) {
        $errors.Add("One or more required Python modules are missing")
    }
}

foreach ($port in @(5000, 5010, 8765, 10001, 10002, 10003)) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($listener) {
        $errors.Add("Port $port is already in use")
    }
}

Write-Host "ASR=$asrMode TTS=$ttsMode start_mode=$startMode"
foreach ($warning in $warnings) { Write-Warning $warning }
foreach ($errorMessage in $errors) { Write-Error $errorMessage -ErrorAction Continue }

if ($errors.Count -gt 0) {
    Write-Host "Preflight failed with $($errors.Count) blocking issue(s)."
    exit 1
}

Write-Host "Preflight passed. Xiaoru is ready for the local demo."
exit 0
