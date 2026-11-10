# Shared launcher for Fay and TryOnWardrobe. The model outlives either UI.
function Get-SharedFashnPaths {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
    $InstallRoot = $env:FAY_FASHN_VTON_ROOT
    if (-not $InstallRoot) { $InstallRoot = Join-Path (Split-Path -Parent $ProjectRoot) 'fashn-vton-1.5' }
    $DesktopRoot = Split-Path -Parent $InstallRoot
    $StateRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'FayWardrobe\FashnShared'
    return @{
        InstallRoot = $InstallRoot
        Python = Join-Path $DesktopRoot 'fashn-vton-env312\Scripts\python.exe'
        Weights = Join-Path $InstallRoot 'weights'
        Project = $ProjectRoot
        StateRoot = $StateRoot
        TokenFile = Join-Path $StateRoot 'service.token'
        StateFile = Join-Path $StateRoot 'service.json'
    }
}

function Get-SharedFashnOrigin {
    $WardrobeUrl = $env:WARDROBE_MODEL_URL
    $FayUrl = $env:FAY_TRYON_LOCAL_URL
    $Origins = @()
    foreach ($Value in @($WardrobeUrl, $FayUrl)) {
        if (-not $Value) { continue }
        $Uri = [Uri]$Value
        if (-not $Uri.IsAbsoluteUri -or $Uri.Scheme -ne 'http' -or -not $Uri.IsLoopback -or $Uri.UserInfo -or $Uri.Query -or $Uri.Fragment -or $Uri.AbsolutePath -notin @('/', '/tryon')) {
            throw 'Shared model URLs must be loopback HTTP origins or /tryon endpoints.'
        }
        $Origins += $Uri.GetLeftPart([UriPartial]::Authority).TrimEnd('/')
    }
    $Unique = @($Origins | Select-Object -Unique)
    if ($Unique.Count -gt 1) { throw 'FAY_TRYON_LOCAL_URL and WARDROBE_MODEL_URL point to different models. Set both to port 7860.' }
    if ($Unique.Count -eq 1) { return $Unique[0] }
    return 'http://127.0.0.1:7860'
}

function Get-SharedFashnHealth {
    param([string]$Origin)
    try {
        $Health = Invoke-RestMethod -Uri ($Origin + '/health') -TimeoutSec 2
        if ($Health.service -in @('fashn-vton-1.5', 'wardrobe-fashn-vton-1.5')) { return $Health }
    } catch { }
    return $null
}

function Test-SharedFashnProtocol {
    param($Health)
    return [bool]($Health -and $Health.ok -and $Health.protocol_version -eq 2 -and $Health.serialized_inference -eq $true -and 'garment_image' -in $Health.accepted_inputs -and 'garment_image_url' -in $Health.accepted_inputs)
}

function Get-SharedFashnToken {
    param($Paths, [switch]$Create)
    $Explicit = @(@($env:WARDROBE_MODEL_TOKEN, $env:FAY_TRYON_LOCAL_TOKEN) | Where-Object { $_ } | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique)
    if ($Explicit.Count -gt 1) { throw 'Fay and Wardrobe model tokens conflict. Do not run them with different shared credentials.' }
    $Stored = ''
    if (Test-Path -LiteralPath $Paths.TokenFile -PathType Leaf) {
        $Stored = (Get-Content -LiteralPath $Paths.TokenFile -Raw -Encoding ASCII).Trim()
        if (-not $Stored) { throw 'Shared model credential file is empty.' }
        if ($Stored -notmatch '^[\x21-\x7E]+$') { throw 'Shared model credential must use printable ASCII characters without spaces.' }
    }
    if ($Explicit.Count -eq 1 -and $Stored -and $Explicit[0] -ne $Stored) {
        throw 'The configured model token differs from the shared credential file. Remove the conflicting shell variable or use the stored credential.'
    }
    if ($Stored) { return $Stored }
    $Token = ''
    if ($Explicit.Count -eq 1) {
        $Token = $Explicit[0]
        if ($Token -notmatch '^[\x21-\x7E]+$') { throw 'Shared model credential must use printable ASCII characters without spaces.' }
    }
    if ($Create) {
        if (-not $Token) { $Token = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N') }
        New-Item -ItemType Directory -Path $Paths.StateRoot -Force | Out-Null
        [IO.File]::WriteAllText($Paths.TokenFile, $Token, [Text.Encoding]::ASCII)
    }
    return $Token
}

function Set-SharedFashnClientEnvironment {
    param([string]$Origin, [string]$Token)
    $env:WARDROBE_MODEL_URL = $Origin
    $env:FAY_TRYON_LOCAL_URL = $Origin + '/tryon'
    $env:FAY_TRYON_LOCAL_HEALTH_URL = $Origin + '/health'
    $env:WARDROBE_MODEL_TOKEN = $Token
    $env:FAY_TRYON_LOCAL_TOKEN = $Token
    if (-not $env:FAY_TRYON_LOCAL_TIMEOUT_SECONDS) { $env:FAY_TRYON_LOCAL_TIMEOUT_SECONDS = '600' }
}

function Test-SharedFashnAuthorization {
    param([string]$Origin, [string]$Token)
    $Headers = @{}
    if ($Token) { $Headers.Authorization = 'Bearer ' + $Token }
    try {
        $Health = Invoke-RestMethod -Uri ($Origin + '/health/auth') -Headers $Headers -TimeoutSec 2
        return [bool](Test-SharedFashnProtocol $Health)
    } catch { return $false }
}

function Get-SharedFashnListener {
    param([string]$Origin)
    $Uri = [Uri]$Origin
    return Get-NetTCPConnection -LocalPort $Uri.Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Start-SharedFashnService {
    param([string]$ModelPython = '', [string]$WeightsDir = '', [switch]$NoStart)
    $Paths = Get-SharedFashnPaths
    $Mutex = New-Object Threading.Mutex($false, 'Local\FayWardrobeFashnShared')
    $Held = $false
    $Launched = $null
    try {
        try { $Held = $Mutex.WaitOne(45000) } catch [Threading.AbandonedMutexException] { $Held = $true }
        if (-not $Held) { throw 'Another shared model startup is still running. Try again after it finishes.' }
        $Origin = Get-SharedFashnOrigin
        $Token = Get-SharedFashnToken $Paths
        $Live = @()
        $Candidates = @($Origin, 'http://127.0.0.1:7860', 'http://127.0.0.1:7861') | Group-Object { ([Uri]$_).Port } | ForEach-Object { $_.Group[0] }
        foreach ($Candidate in $Candidates) {
            $Health = Get-SharedFashnHealth $Candidate
            if (-not $Health) { continue }
            if (-not (Test-SharedFashnProtocol $Health)) {
                throw "An old FASHN service is running at $Candidate. Stop its original terminal/service, then restart with the updated launcher. No second model was started."
            }
            $Live += @{ Origin = $Candidate; Health = $Health }
        }
        if ($Live.Count -gt 1) { throw 'Two model services are already running. Stop one before using the shared launcher.' }
        if ($Live.Count -eq 1) {
            $Existing = $Live[0]
            if ($Existing.Origin -ne $Origin) {
                if ($env:WARDROBE_MODEL_URL -or $env:FAY_TRYON_LOCAL_URL) { throw "A model already runs at $($Existing.Origin). Configure both clients to that address instead of starting another model." }
                $Origin = $Existing.Origin
            }
            if ($Existing.Health.initialization_failed) { throw 'The shared model failed to initialize. Stop it, check its log, and restart.' }
            if (-not (Test-SharedFashnAuthorization $Origin $Token)) { throw 'Shared model authentication failed. Use its original shared credential, then restart the client.' }
            Set-SharedFashnClientEnvironment $Origin $Token
            Write-Host "Reusing shared FASHN model: $Origin (no second pipeline)."
            return @{ Origin = $Origin; Started = $false; LogDir = $Paths.StateRoot }
        }
        if ($NoStart) {
            # Keep offline clients aligned with a later model launch's credential.
            $Token = Get-SharedFashnToken $Paths -Create
            Set-SharedFashnClientEnvironment $Origin $Token
            Write-Host "No shared model running at $Origin. Inference is unavailable until the model is started."
            return @{ Origin = $Origin; Started = $false; LogDir = $Paths.StateRoot }
        }
        if (Get-SharedFashnListener $Origin) { throw "Port $(([Uri]$Origin).Port) is occupied by a different service. No model was started." }
        if (-not $ModelPython) { $ModelPython = $Paths.Python }
        if (-not $WeightsDir) { $WeightsDir = $Paths.Weights }
        if (-not (Test-Path -LiteralPath $ModelPython -PathType Leaf)) { throw "Model Python not found: $ModelPython" }
        if (-not (Test-Path -LiteralPath (Join-Path $WeightsDir 'model.safetensors') -PathType Leaf)) { throw "Model weights not found: $WeightsDir" }
        $Token = Get-SharedFashnToken $Paths -Create
        Set-SharedFashnClientEnvironment $Origin $Token
        $env:PYTHONUNBUFFERED = '1'
        $Module = 'agent.tryon.local_service'
        $WorkingRoot = $Paths.Project
        if (-not (Test-Path -LiteralPath (Join-Path $WorkingRoot 'agent\tryon\local_service.py') -PathType Leaf)) { throw 'Local model service module is missing.' }
        $Uri = [Uri]$Origin
        $Arguments = @('-m', $Module, '--weights-dir', ('"' + $WeightsDir + '"'), '--host', $Uri.Host, '--port', [string]$Uri.Port)
        $LogStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
        $Stdout = Join-Path $Paths.StateRoot "model-$LogStamp.stdout.log"
        $Stderr = Join-Path $Paths.StateRoot "model-$LogStamp.stderr.log"
        $Launched = Start-Process -FilePath $ModelPython -ArgumentList $Arguments -WorkingDirectory $WorkingRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
        $Ready = $false
        for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
            $Launched.Refresh()
            if ($Launched.HasExited) { throw "Model startup failed. See $Stderr" }
            if (Test-SharedFashnAuthorization $Origin $Token) { $Ready = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $Ready) { throw "Model health/auth check failed. See $Stderr" }
        $Launcher = Get-CimInstance Win32_Process -Filter "ProcessId = $($Launched.Id)"
        $Listener = Get-SharedFashnListener $Origin
        $Process = Get-CimInstance Win32_Process -Filter "ProcessId = $($Listener.OwningProcess)"
        $State = @{ origin = $Origin; launcher_id = $Launched.Id; launcher_created = $Launcher.CreationDate.ToString('o'); listener_id = $Process.ProcessId; listener_created = $Process.CreationDate.ToString('o'); module = $Module; stderr_log = $Stderr }
        [IO.File]::WriteAllText($Paths.StateFile, ($State | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
        $Launched = $null
        Write-Host "Shared FASHN model started: $Origin. It stays running when either app exits."
        Write-Host "Model log: $Stderr"
        return @{ Origin = $Origin; Started = $true; LogDir = $Paths.StateRoot }
    } finally {
        if ($Launched) {
            $Launched.Refresh()
            if (-not $Launched.HasExited) { & taskkill.exe /PID $Launched.Id /T /F | Out-Null }
        }
        if ($Held) { $Mutex.ReleaseMutex() }
        $Mutex.Dispose()
    }
}

function Stop-SharedFashnService {
    $Paths = Get-SharedFashnPaths
    $Mutex = New-Object Threading.Mutex($false, 'Local\FayWardrobeFashnShared')
    $Held = $false
    try {
        try { $Held = $Mutex.WaitOne(45000) } catch [Threading.AbandonedMutexException] { $Held = $true }
        if (-not $Held) { throw 'Shared model startup is still running.' }
        if (-not (Test-Path -LiteralPath $Paths.StateFile -PathType Leaf)) { Write-Host 'No managed shared model state. Nothing was stopped.'; return }
        $State = Get-Content -LiteralPath $Paths.StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $Listener = Get-SharedFashnListener $State.origin
        $Process = Get-CimInstance Win32_Process -Filter "ProcessId = $($State.listener_id)"
        $Launcher = Get-CimInstance Win32_Process -Filter "ProcessId = $($State.launcher_id)"
        if (-not $Process -and -not $Listener) { Write-Host 'Shared model is already stopped.'; return }
        $ParentMatches = $false
        if ($Process) { $ParentMatches = ($Process.ParentProcessId -eq [int]$State.launcher_id -or [int]$State.listener_id -eq [int]$State.launcher_id) }
        if (-not $Process -or -not $Launcher -or -not $Listener -or -not $ParentMatches -or $Listener.OwningProcess -ne $State.listener_id -or $Process.CreationDate.ToString('o') -ne $State.listener_created -or $Launcher.CreationDate.ToString('o') -ne $State.launcher_created -or $Launcher.CommandLine -notmatch ('-m\s+' + [regex]::Escape($State.module)) -or $Process.CommandLine -notmatch ('-m\s+' + [regex]::Escape($State.module)) -or $State.module -notin @('model_service.main', 'agent.tryon.local_service')) {
            throw 'The shared model process identity changed. Nothing was stopped; inspect the original service terminal.'
        }
        if (-not (Test-SharedFashnProtocol (Get-SharedFashnHealth $State.origin))) { throw 'Model protocol identity changed. Nothing was stopped.' }
        & taskkill.exe /PID $State.launcher_id /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not stop the managed shared model process.' }
        Write-Host 'Shared model stopped. Both apps can remain open, but try-on is unavailable until the next launcher startup.'
    } finally {
        if ($Held) { $Mutex.ReleaseMutex() }
        $Mutex.Dispose()
    }
}
