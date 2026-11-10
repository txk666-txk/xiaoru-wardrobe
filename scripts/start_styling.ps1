$ErrorActionPreference = "Stop"
$styleBackend = [Environment]::GetEnvironmentVariable("FAY_STYLE_VISION_BACKEND")
if ([string]::IsNullOrWhiteSpace($styleBackend)) { $styleBackend = "ollama" }
if ($styleBackend -eq "openai_local") {
    # Adapted model servers are started in their own training/inference environment.
    # Health-check only; do not also load Ollama and compete for GPU memory.
    $styleAdaptedOrigin = [Environment]::GetEnvironmentVariable("FAY_STYLE_VISION_URL")
    if ([string]::IsNullOrWhiteSpace($styleAdaptedOrigin)) { $styleAdaptedOrigin = "http://127.0.0.1:8001/v1" }
    $styleAdaptedUri = [Uri]$styleAdaptedOrigin
    if ($styleAdaptedUri.Scheme -ne "http" -or $styleAdaptedUri.Host -notin @("127.0.0.1", "localhost", "[::1]", "::1") -or
        $styleAdaptedUri.UserInfo -or $styleAdaptedUri.AbsolutePath.TrimEnd('/') -notin @("", "/v1") -or
        $styleAdaptedUri.Query -or $styleAdaptedUri.Fragment) {
        throw "FAY_STYLE_VISION_URL must be a loopback HTTP origin, optionally ending in /v1."
    }
    $styleAdaptedModel = [Environment]::GetEnvironmentVariable("FAY_STYLE_VISION_MODEL")
    try {
        $styleAdaptedStatus = Invoke-RestMethod -Uri ($styleAdaptedOrigin.TrimEnd('/') + "/models") -TimeoutSec 3 -MaximumRedirection 0
        if (-not [string]::IsNullOrWhiteSpace($styleAdaptedModel) -and
            @($styleAdaptedStatus.data | ForEach-Object { $_.id }) -contains $styleAdaptedModel) {
            Write-Host "Local adapted vision ready: $styleAdaptedModel"
        } else {
            Write-Warning "Set FAY_STYLE_VISION_MODEL to an ID exposed by the local model server."
        }
    } catch {
        Write-Warning "Local adapted vision is not ready. Start its model server first. Text styling still works."
    }
    return
}
if ($styleBackend -ne "ollama") { throw "FAY_STYLE_VISION_BACKEND must be ollama or openai_local." }
# Reuse the existing Ollama service. Never start a second GPU model process.
$styleOrigin = [Environment]::GetEnvironmentVariable("FAY_STYLE_OLLAMA_URL")
if ([string]::IsNullOrWhiteSpace($styleOrigin)) { $styleOrigin = "http://127.0.0.1:11434" }
$styleUri = [Uri]$styleOrigin
if ($styleUri.Scheme -ne "http" -or $styleUri.Host -notin @("127.0.0.1", "localhost", "[::1]", "::1") -or $styleUri.UserInfo -or $styleUri.AbsolutePath -ne "/" -or $styleUri.Query -or $styleUri.Fragment) {
    throw "FAY_STYLE_OLLAMA_URL must be a loopback HTTP origin."
}
function Test-StyleOllama {
    try {
        return Invoke-RestMethod -Uri ($styleOrigin.TrimEnd('/') + "/api/tags") -TimeoutSec 3
    } catch { return $null }
}
$styleStatus = Test-StyleOllama
if (-not $styleStatus) {
    $styleCommand = Get-Command ollama -ErrorAction SilentlyContinue
    $styleExe = if ($styleCommand) { $styleCommand.Source } else { Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
    if (-not (Test-Path -LiteralPath $styleExe)) {
        Write-Warning "Ollama is not installed. Text styling still works; install Ollama for photo styling."
        return
    }
    $styleOldHost = [Environment]::GetEnvironmentVariable("OLLAMA_HOST")
    try {
        $env:OLLAMA_HOST = $styleUri.Authority
        Start-Process -FilePath $styleExe -ArgumentList "serve" -WindowStyle Hidden | Out-Null
    } finally {
        [Environment]::SetEnvironmentVariable("OLLAMA_HOST", $styleOldHost)
    }
    for ($styleAttempt = 0; $styleAttempt -lt 15 -and -not $styleStatus; $styleAttempt++) {
        Start-Sleep -Milliseconds 500
        $styleStatus = Test-StyleOllama
    }
}
$styleModel = [Environment]::GetEnvironmentVariable("FAY_STYLE_VISION_MODEL")
if ([string]::IsNullOrWhiteSpace($styleModel)) { $styleModel = "qwen3-vl:2b-instruct" }
if ($styleStatus -and @($styleStatus.models | ForEach-Object { $_.name }) -contains $styleModel) {
    Write-Host "Local styling ready: $styleModel (CPU). FASHN retains the GPU."
} else {
    Write-Warning "Photo styling is not ready. Run scripts/setup_styling.ps1 once. Text styling still works."
}
