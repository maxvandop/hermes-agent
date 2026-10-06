# start-host-commands.ps1
# Start (or restart) the host-commands MCP server.
# Safe to run multiple times — kills any existing instance first.
#
# Usage (PowerShell on the Windows host):
#   -ExecutionPolicy Bypass -File "$env:USERPROFILE\.hermes\host-commands\start-host-commands.ps1"

$ErrorActionPreference = "Stop"
$DataDir = Join-Path $env:USERPROFILE ".hermes\host-commands"
$ServerPy = Join-Path $DataDir "server.py"

# 1. Verify server.py exists
if (-not (Test-Path $ServerPy)) {
    Write-Error "server.py not found at $ServerPy"
    exit 1
}

# 2. Kill any existing host-commands server (match by command line)
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*host-commands*server.py*" }
if ($existing) {
    foreach ($proc in $existing) {
        Write-Host "Stopping existing server (PID $($proc.ProcessId))..." -ForegroundColor Yellow
        Stop-Process -Id $proc.ProcessId -Force
    }
    Start-Sleep -Seconds 2
} else {
    Write-Host "No existing server found." -ForegroundColor Gray
}

# 3. Verify mcp package is installed
& python -c "import mcp" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing mcp package..." -ForegroundColor Yellow
    & python -m pip install -r (Join-Path $DataDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install mcp package"
        exit 1
    }
}

# 4. Start the server (hidden window, detached, with logging)
$LogFile = Join-Path $DataDir "server.log"
$ErrLog  = Join-Path $DataDir "server-error.log"
$proc = Start-Process python -ArgumentList "server.py" `
    -WorkingDirectory $DataDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError $ErrLog `
    -PassThru
Write-Host "Server starting (PID $($proc.Id))..." -ForegroundColor Cyan
Write-Host "  Logs: $LogFile" -ForegroundColor Gray
Write-Host "  Errors: $ErrLog" -ForegroundColor Gray

# 5. Wait for port 8090 to come up (max 15s)
$up = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect("127.0.0.1", 8090)
        $tcp.Close()
        $up = $true
        break
    } catch {
        # port not ready yet
    }
}

if ($up) {
    Write-Host "Server is UP on port 8090 (PID $($proc.Id))" -ForegroundColor Green
} else {
    Write-Warning "Server may not be up yet (port 8090 not responding after 15s). Check the process."
    exit 1
}
