param(
    [string]$FashnRoot = "",
    [string]$EnvironmentRoot = "",
    [string]$WeightsRoot = "",
    [string]$BasePython = "",
    [switch]$IgnoreProxy
)

$ErrorActionPreference = "Stop"
$installParent = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $FashnRoot) { $FashnRoot = $env:FAY_FASHN_VTON_ROOT }
if (-not $FashnRoot) { $FashnRoot = Join-Path $installParent "fashn-vton-1.5" }
if (-not $EnvironmentRoot) { $EnvironmentRoot = Join-Path $installParent "fashn-vton-env312" }
if (-not $WeightsRoot) { $WeightsRoot = $env:FAY_FASHN_VTON_WEIGHTS_DIR }
if (-not $WeightsRoot) { $WeightsRoot = Join-Path $FashnRoot "weights" }

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $false)][string[]]$Arguments = @()
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Native command failed (exit code $LASTEXITCODE): $Executable $($Arguments -join ' ')"
    }
}

function Get-PythonVersion {
    param([Parameter(Mandatory = $true)][string]$Executable)

    $versionText = & $Executable --version
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read Python version: $Executable"
    }
    $versionText = ($versionText | Select-Object -Last 1).ToString().Trim()
    if ($versionText -notmatch '^Python (\d+)\.(\d+)') {
        throw "Unable to parse Python version '$versionText': $Executable"
    }
    return New-Object System.Version([int]$Matches[1], [int]$Matches[2])
}

if (-not (Test-Path $FashnRoot -PathType Container)) {
    throw "FASHN source directory does not exist: $FashnRoot"
}

$python = Join-Path $EnvironmentRoot "Scripts\python.exe"
if (Test-Path $python -PathType Leaf) {
    $pythonVersion = Get-PythonVersion -Executable $python
    if ($pythonVersion -lt (New-Object System.Version(3, 10))) {
        throw "Existing FASHN environment uses Python $pythonVersion; FASHN VTON requires Python >= 3.10. Remove or replace '$EnvironmentRoot', or pass -EnvironmentRoot for a new environment."
    }
} else {
    if ([string]::IsNullOrWhiteSpace($BasePython)) {
        $BasePython = "python"
    }
    if (-not (Get-Command $BasePython -ErrorAction SilentlyContinue)) {
        throw "Python executable not found: $BasePython. Install Python 3.10+ or pass -BasePython with its full path."
    }
    $baseVersion = Get-PythonVersion -Executable $BasePython
    if ($baseVersion -lt (New-Object System.Version(3, 10))) {
        throw "Python used to create the environment is $baseVersion; FASHN VTON requires Python >= 3.10. Pass -BasePython for Python 3.10+."
    }
    Invoke-Native -Executable $BasePython -Arguments @("-m", "venv", $EnvironmentRoot)
    if (-not (Test-Path $python -PathType Leaf)) {
        throw "Python was not found after creating the virtual environment: $python"
    }
}

# Preserve the user's network setup unless explicitly diagnosing a stale proxy.
if ($IgnoreProxy) {
    $env:HTTP_PROXY = ""
    $env:HTTPS_PROXY = ""
    $env:ALL_PROXY = ""
    $env:NO_PROXY = "*"
}

Invoke-Native -Executable $python -Arguments @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")
Invoke-Native -Executable $python -Arguments @(
    "-m", "pip", "install", "--index-url", "https://download.pytorch.org/whl/cu128",
    "torch==2.8.0+cu128", "torchvision==0.23.0+cu128"
)
Invoke-Native -Executable $python -Arguments @(
    "-m", "pip", "install", "opencv-python", "safetensors", "huggingface_hub", "pillow",
    "numpy", "tqdm", "einops", "onnxruntime-gpu", "requests", "fashn-human-parser"
)

# Install the checked-out FASHN package so the local service can import it.
Invoke-Native -Executable $python -Arguments @("-m", "pip", "install", "--upgrade", $FashnRoot)

Invoke-Native -Executable $python -Arguments @(
    (Join-Path $PSScriptRoot "inspect_fashn_runtime.py")
)

Push-Location $FashnRoot
try {
    Invoke-Native -Executable $python -Arguments @("scripts\download_weights.py", "--weights-dir", $WeightsRoot)
} finally {
    Pop-Location
}

Write-Host "FASHN VTON setup completed. Set FAY_FASHN_VTON_ROOT, FAY_FASHN_VTON_PYTHON and FAY_FASHN_VTON_WEIGHTS_DIR, then use scripts/start_shared_model.ps1."
