param(
    [ValidateSet("start", "stop", "restart")]
    [string]$Command = "start",
    [switch]$SkipPreflight,
    [switch]$NoLocalTryOn,
    [switch]$NoLocalStyling,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

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
    throw "Project Python runtime was not found under .venv. Use -PythonExe to select an existing environment."
}
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path

$requiredLocalFiles = @("system.conf", "config.json", "qa.csv")
$missing = $requiredLocalFiles | Where-Object { -not (Test-Path -LiteralPath (Join-Path $projectRoot $_)) }
if ($missing) {
    throw "Missing local config: $($missing -join ', '). Run scripts/initialize_local_config.ps1 first."
}

if (-not $SkipPreflight -and $Command -in @("start", "restart")) {
    & (Join-Path $PSScriptRoot "demo_preflight.ps1") -PythonExe $PythonExe
    if ($LASTEXITCODE -ne 0) {
        throw "Demo preflight failed. Fix the reported issue or use -SkipPreflight for diagnosis only."
    }
}

function Get-EnvOrDefault {
    param(
        [string]$Name,
        [string]$Default
    )
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $Default
    }
    return $value
}

function Start-LocalTryOn {
    if ($Command -notin @("start", "restart")) { return }
    $installParent = Split-Path -Parent $projectRoot
    $fashnRoot = Get-EnvOrDefault "FAY_FASHN_VTON_ROOT" (Join-Path $installParent "fashn-vton-1.5")
    $configuredTryOnPython = [Environment]::GetEnvironmentVariable("FAY_FASHN_VTON_PYTHON")
    if ([string]::IsNullOrWhiteSpace($configuredTryOnPython)) {
        $tryonPythonCandidates = @(
            (Join-Path $installParent "fashn-vton-env312\Scripts\python.exe"),
            (Join-Path $installParent "fashn-vton-env\Scripts\python.exe")
        )
        $tryonPython = $tryonPythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $tryonPython) { $tryonPython = $tryonPythonCandidates[0] }
    } else {
        $tryonPython = $configuredTryOnPython
    }
    $weights = Get-EnvOrDefault "FAY_FASHN_VTON_WEIGHTS_DIR" (Join-Path $fashnRoot "weights")
    # An installed local model is enough to enable one-command startup.
    # Preserve an explicitly selected cloud/disabled provider.
    $provider = [Environment]::GetEnvironmentVariable("FAY_TRYON_PROVIDER")
    if ([string]::IsNullOrWhiteSpace($provider)) {
        if ((Test-Path -LiteralPath $tryonPython) -and (Test-Path -LiteralPath (Join-Path $weights "model.safetensors"))) {
            $provider = "fashn_local"
        } else {
            $provider = "disabled"
        }
    }
    if ($provider.Trim().ToLowerInvariant() -notin @("fashn_local", "local_fashn", "fashn-vton", "fashn_vton_local")) {
        return
    }
    $env:FAY_TRYON_PROVIDER = $provider.Trim()
    $sharedHelper = Join-Path $PSScriptRoot "shared_fashn_service.ps1"
    if (-not (Test-Path -LiteralPath $sharedHelper -PathType Leaf)) {
        throw "Shared model launcher missing: $sharedHelper"
    }
    . $sharedHelper
    $sharedModel = Start-SharedFashnService -ModelPython $tryonPython -WeightsDir $weights -NoStart:$NoLocalTryOn
    Write-Host "The apps share $($sharedModel.Origin). Exiting this app does not stop the model."
}

if ($Command -in @("start", "restart") -and -not $NoLocalStyling) {
    & (Join-Path $PSScriptRoot "start_styling.ps1")
}
Start-LocalTryOn
& $pythonExe "main.py" $Command
$exitCode = $LASTEXITCODE
exit $exitCode
