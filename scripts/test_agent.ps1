param(
    [switch]$SkipUi,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

$pythonCandidates = @(
    (Join-Path $projectRoot ".venv\python.exe"),
    (Join-Path $projectRoot ".venv\Scripts\python.exe")
)
if (-not $PythonExe) {
    $PythonExe = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if ($PythonExe -and -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable does not exist: $PythonExe"
}
if (-not $pythonExe) {
    throw "Python runtime was not found under .venv. Use -PythonExe to select an existing environment."
}

$pythonSuites = @(
    "test.test_release_assets",
    "test.test_public_fashion_catalog",
    "test.test_catalog_images",
    "test.test_local_catalog_import",
    "test.test_audio_conversion",
    "test.test_config_loading",
    "test.test_styling_vision_adapter",
    "test.test_styling_vision_evaluation",
    "test.test_styling_vision_sft",
    "test.test_styling",
    "test.test_styling_conversation",
    "test.test_styling_planner",
    "test.test_styling_langgraph",
    "test.test_ecommerce_agent",
    "test.test_service_policy",
    "test.test_large_order_review",
    "test.test_commerce_backend",
    "test.test_workbench_security",
    "test.test_fashn_local_provider",
    "test.test_fashn_local_service",
    "test.test_virtual_tryon"
)

Write-Host "Running Xiaoru Agent backend regression..."
& $pythonExe -X utf8 -m unittest @pythonSuites
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if (-not $SkipUi) {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        throw "Node.js is required for UI regression tests. Install Node.js or rerun with -SkipUi."
    }
    $uiSuites = @(
        "test/test_styling_chat_ui.js",
        "test/test_styling_ui.js",
        "test/test_tryon_upload_ui.js",
        "test/test_workbench_security_ui.js"
    )
    Write-Host "Running Xiaoru Agent UI regression..."
    & $node.Source --test @uiSuites
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Write-Host "Xiaoru Agent regression passed."
