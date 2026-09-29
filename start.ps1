param(
    [switch]$Restart,
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$DataDir = Join-Path $Root "data"
$RunDir = Join-Path ([IO.Path]::GetTempPath()) "RemoteCodex"
$AdminLink = Join-Path $RunDir "admin-console.txt"
$PidFile = Join-Path $RunDir "agent.pid"

if (-not (Test-Path $Python)) {
    Write-Host "[RemoteCodex] Creating virtual environment..."
    python -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Failed to create Python virtual environment." }
}

# Dependency probes are expected to fail on a fresh machine. Keep PowerShell
# from turning their stderr tracebacks into terminating NativeCommandError.
$CoreDependenciesInstalled = $false
$TerminalDependencyInstalled = $false
$PreviousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Python -c "import websockets, psutil, mss, PIL, pyautogui" > $null 2>$null
if ($LASTEXITCODE -eq 0) {
    $CoreDependenciesInstalled = $true
}

& $Python -c "import winpty" > $null 2>$null
if ($LASTEXITCODE -eq 0) {
    $TerminalDependencyInstalled = $true
}
$ErrorActionPreference = $PreviousErrorActionPreference

if (-not $CoreDependenciesInstalled) {
    Write-Host "[RemoteCodex] Installing dependencies..."
    & $Python -m pip install -e ".[monitor,desktop]"
    if ($LASTEXITCODE -ne 0) { throw "Failed to install backend dependencies." }
}

if (-not $TerminalDependencyInstalled) {
    Write-Host "[RemoteCodex] Installing terminal PTY support..."
    & $Python -m pip install "pywinpty>=2.0.13"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "pywinpty installation failed. Terminal will use pipe mode."
    }
}

$ExistingPid = 0
$Existing = $null
if (Test-Path $PidFile) {
    $rawPid = (Get-Content $PidFile -Raw).Trim()
    if ([int]::TryParse($rawPid, [ref]$ExistingPid) -and $ExistingPid -gt 0) {
        $Existing = Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue
    }
}

$ExpectedPort = 7800
try {
    $ConfigPath = Join-Path $Root "config.mobile.json"
    if (Test-Path $ConfigPath) {
        $Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
        $ExpectedPort = [int]$Config.server.port
    }
} catch {
    # Fall back to the default configured port.
}

$PortOwners = @()
try {
    $PortOwners = @(Get-NetTCPConnection -LocalPort $ExpectedPort -State Listen -ErrorAction Stop |
        Select-Object -ExpandProperty OwningProcess -Unique |
        Where-Object { [int]$_ -gt 0 })
} catch {
    # Get-NetTCPConnection can require elevation; netstat is a usable fallback.
    $PortOwners = @(netstat -ano -p tcp 2>$null |
        Select-String (":{0} .*LISTENING" -f $ExpectedPort) |
        ForEach-Object {
            if ($_.Line -match "\sLISTENING\s+(\d+)\s*$") { [int]$Matches[1] }
        } |
        Where-Object { $_ -gt 0 } |
        Select-Object -Unique)
}
$PortListening = $PortOwners.Count -gt 0

if ($Existing -and $Existing.ProcessName -match 'python' -and $PortListening -and -not $Restart) {
    Write-Host "[RemoteCodex] Agent is already running (PID $ExistingPid)."
    if (-not $NoOpen -and (Test-Path $AdminLink)) {
        $ExistingUrl = (Get-Content $AdminLink)[1]
        if ($ExistingUrl) {
            try {
                Start-Process $ExistingUrl -WindowStyle Hidden
            } catch {
                Write-Host "[RemoteCodex] Admin console: $ExistingUrl"
            }
        }
    }
    exit 0
}

if ($PortListening -and -not $Restart -and -not $Existing) {
    throw "Port $ExpectedPort is already in use. Run start.bat to restart RemoteCodex."
}

if ($Restart -and $PortListening) {
    foreach ($PortOwner in $PortOwners) {
        $ownerId = [int]$PortOwner
        if ($ownerId -gt 0 -and $ownerId -ne $PID) {
            $ownerProcess = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
            if (-not $ownerProcess -or $ownerProcess.ProcessName -notmatch 'python') {
                throw "Port $ExpectedPort is owned by non-Agent process $ownerId ($($ownerProcess.ProcessName)). Stop it manually before restarting RemoteCodex."
            }
            Write-Host "[RemoteCodex] Stopping process $ownerId on port $ExpectedPort..."
            Stop-Process -Id $ownerId -Force -ErrorAction Stop
        }
    }
    Start-Sleep -Milliseconds 300
    $stillListening = @(netstat -ano -p tcp 2>$null |
        Select-String (":{0}\s+.*LISTENING" -f $ExpectedPort)).Count -gt 0
    if ($stillListening) {
        throw "Port $ExpectedPort is still in use. Restart RemoteCodex from an elevated terminal."
    }
}

if ($Existing) {
    if ($Restart -or $Existing.Path -eq $Python) {
        Write-Host "[RemoteCodex] Stopping old Agent process $ExistingPid..."
        Stop-Process -Id $ExistingPid -Force -ErrorAction SilentlyContinue
        $Existing = $null
    } else {
        # Preserve unrelated Python processes and use a replacement PID file.
        $PidFile = Join-Path $RunDir ("agent.{0}.pid" -f (Get-Date -Format 'yyyyMMddHHmmss'))
    }
}

if (-not $Existing -and (Test-Path $PidFile)) {
    try {
        Remove-Item $PidFile -Force
    } catch {
        $PidFile = Join-Path $RunDir ("agent.{0}.pid" -f (Get-Date -Format 'yyyyMMddHHmmss'))
    }
}

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
if (Test-Path $AdminLink) { [System.IO.File]::WriteAllText($AdminLink, "") }

$OutLog = Join-Path $RunDir "agent.out.log"
$ErrLog = Join-Path $RunDir "agent.err.log"
$InLog = Join-Path $RunDir "agent.in.log"
Write-Host "[RemoteCodex] Starting agent..."
[System.IO.File]::WriteAllText($InLog, "")
[System.IO.File]::WriteAllText($OutLog, "")
[System.IO.File]::WriteAllText($ErrLog, "")
$Process = Start-Process `
    -FilePath $Python `
    -ArgumentList @("-u", "-m", "app", "serve", "--config", "config.mobile.json") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardInput $InLog `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru
[System.IO.File]::WriteAllText($PidFile, [string]$Process.Id)

$AdminUrl = $null
for ($i = 0; $i -lt 200; $i++) {
    if ($Process.HasExited) { break }
    if (Test-Path $AdminLink) {
        $AdminUrl = (Get-Content $AdminLink | Select-Object -Last 1)
        if ($AdminUrl) { break }
    }
    Start-Sleep -Milliseconds 300
}

if (-not $AdminUrl) {
    $ExitCode = $null
    if ($Process.HasExited) { $ExitCode = $Process.ExitCode }
    if (-not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $Process.Id -Timeout 5 -ErrorAction SilentlyContinue
    }
    Write-Host "[RemoteCodex] Agent failed to start$(if ($null -ne $ExitCode) { " (exit code $ExitCode)" }):" -ForegroundColor Red
    $Errors = @(if (Test-Path $ErrLog) { Get-Content $ErrLog -Tail 20 -ErrorAction SilentlyContinue })
    $Output = @(if (Test-Path $OutLog) { Get-Content $OutLog -Tail 20 -ErrorAction SilentlyContinue })
    if ($Errors.Count -gt 0) { $Errors }
    if ($Output.Count -gt 0) {
        Write-Host "[RemoteCodex] Last output:"
        $Output
    }
    if (Test-Path $PidFile) { Remove-Item $PidFile -Force -ErrorAction SilentlyContinue }
    exit 1
}

Write-Host "[RemoteCodex] Agent running (PID $($Process.Id))."
Write-Host "[RemoteCodex] Admin console: $AdminUrl"
Write-Host "[RemoteCodex] Stop with stop.bat"
if (-not $NoOpen) {
    try {
        Start-Process $AdminUrl -WindowStyle Hidden
    } catch {
        Write-Host "[RemoteCodex] Open the admin console manually from the link above."
    }
}
