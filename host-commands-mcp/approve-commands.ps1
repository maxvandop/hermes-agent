# approve-commands.ps1
# Interactive approval loop for host-commands-mcp (see server.py).
# Run this in a terminal you keep an eye on. Polls pending.json every few
# seconds; for each new request it shows Hermes's command + stated reason
# and asks y/N. Approving runs the command once immediately AND appends the
# exact command string to allowlist.json, so identical future calls from
# Hermes execute automatically without asking again.
#
# Usage: .\approve-commands.ps1

$ErrorActionPreference = "Stop"
$DataDir = Join-Path $env:USERPROFILE ".hermes\host-commands"
$PendingPath = Join-Path $DataDir "pending.json"
$AllowlistPath = Join-Path $DataDir "allowlist.json"

New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
if (-not (Test-Path $PendingPath)) { Set-Content -Path $PendingPath -Value '{}' -Encoding UTF8 }
if (-not (Test-Path $AllowlistPath)) { Set-Content -Path $AllowlistPath -Value '[]' -Encoding UTF8 }

Write-Host "Watching for Hermes command approval requests (polling every 3s). Ctrl+C to stop." -ForegroundColor Cyan

while ($true) {
    # -Encoding UTF8 on every read/write below -- PowerShell 5.1's unqualified
    # Set-Content defaults to the system ANSI codepage, which silently mangles
    # any non-ASCII character (e.g. an em-dash in a "reason" string) into a
    # byte sequence Python's strict utf-8 reader rejects, breaking every
    # run_command/list_pending_commands call until the file is repaired.
    $pending = Get-Content $PendingPath -Raw -Encoding UTF8 | ConvertFrom-Json
    # Filter out blank names: re-parsing a JSON object that was emptied down to
    # `{}` can yield one phantom property whose Name is "" (observed with this
    # PowerShell version's ConvertFrom-Json) -- never treat that as a real request.
    $ids = @($pending.PSObject.Properties.Name) | Where-Object { $_ }

    foreach ($id in $ids) {
        $entry = $pending.$id
        Write-Host ""
        Write-Host "Hermes wants to run:" -ForegroundColor Yellow
        Write-Host "  $($entry.command)" -ForegroundColor White
        Write-Host "  Reason: $($entry.reason)" -ForegroundColor Gray
        Write-Host "  Requested: $($entry.requested_at)" -ForegroundColor Gray
        $answer = Read-Host "Approve and run? (y/N)"

        if ($answer -eq "y") {
            Write-Host "Running..." -ForegroundColor Cyan
            $output = & powershell.exe -NoLogo -NoProfile -NonInteractive -Command $entry.command 2>&1
            $output | ForEach-Object { Write-Host $_ }

            # Build the list via `foreach`, not `@(... | ConvertFrom-Json)` --
            # wrapping that pipeline directly in `@()` was observed to corrupt an
            # empty `[]` file into a 1-element array containing a bogus nested
            # object instead of a clean empty list.
            $parsedAllowlist = Get-Content $AllowlistPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $allowlist = New-Object System.Collections.Generic.List[string]
            foreach ($item in $parsedAllowlist) { if ($item) { $allowlist.Add([string]$item) } }

            if (-not $allowlist.Contains($entry.command)) {
                $allowlist.Add($entry.command)
                ConvertTo-Json -InputObject $allowlist -Depth 5 | Set-Content $AllowlistPath -Encoding UTF8
                Write-Host "Added to allowlist -- future identical calls will run automatically." -ForegroundColor Green
            }
        } else {
            Write-Host "Denied." -ForegroundColor Red
        }

        # Re-read + remove just this one id, to minimize clobbering entries
        # the server might add concurrently while we were waiting on input.
        $current = Get-Content $PendingPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $current.PSObject.Properties.Remove($id)
        ConvertTo-Json -InputObject $current -Depth 5 | Set-Content $PendingPath -Encoding UTF8
    }

    Start-Sleep -Seconds 3
}
