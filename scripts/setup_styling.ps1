param([string]$Model = "qwen3-vl:2b-instruct")
$ErrorActionPreference = "Stop"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    throw "Install Ollama from https://ollama.com/download/windows first."
}
Write-Host "Downloading free local vision model: $Model"
& ollama pull $Model
if ($LASTEXITCODE -ne 0) { throw "Model download failed. Run this command again to resume." }
Write-Host "Ready. Fay uses CPU vision to keep GPU memory available for FASHN."
