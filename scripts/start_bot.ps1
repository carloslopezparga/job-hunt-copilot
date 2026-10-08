<#
.SYNOPSIS
    Start the job-hunt-copilot Telegram bot (`jobhunt bot`) and keep it running.

.DESCRIPTION
    Activates the project's virtual environment, reads TELEGRAM_BOT_TOKEN and
    TELEGRAM_CHAT_ID from your user environment variables and runs `jobhunt bot`.
    The bot answers only your chat, searches once a day by itself (09:00 by default)
    and never sends anything to employers or LinkedIn.

    - Only one copy runs at a time (a second start exits quietly), so Telegram never
      sees two bots polling with the same token.
    - If the bot crashes it is restarted after a short pause; if it keeps dying within
      seconds of starting (bad token, broken profile.yaml...) the script gives up.
    - Output goes to logs\bot_<date>.log (files older than 30 days are removed).

.PARAMETER RepoDir
    Repository root. Defaults to the folder that contains this scripts\ directory.

.PARAMETER DailyAt
    Local time (HH:MM) of the automatic daily search. Default: JOBHUNT_BOT_DAILY_AT or 09:00.

.PARAMETER NoDaily
    Do not run the automatic daily search; the bot only answers commands.

.PARAMETER RestartDelaySeconds
    Pause before restarting a crashed bot. Default: 15.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\path\to\job-hunt-copilot\scripts\start_bot.ps1

.EXAMPLE
    .\scripts\start_bot.ps1 -DailyAt 08:30
#>
[CmdletBinding()]
param(
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
    [string]$DailyAt = "",
    [switch]$NoDaily,
    [int]$RestartDelaySeconds = 15
)

$ErrorActionPreference = "Stop"
$exitCode = 1
Set-Location -Path $RepoDir

# Single instance per user session.
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, "Local\job-hunt-copilot-bot", [ref]$createdNew)
if (-not $createdNew) {
    Write-Output "The bot is already running; nothing to do."
    exit 0
}

# Logs: one file per day, older than 30 days removed.
$logDir = Join-Path $RepoDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Get-ChildItem -Path $logDir -Filter "bot_*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue
Start-Transcript -Path (Join-Path $logDir ("bot_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))) -Append | Out-Null

try {
    # 1) Virtual environment (Windows layout first, then the Linux/macOS one for testing).
    $activate = @(
        (Join-Path $RepoDir ".venv\Scripts\Activate.ps1"),
        (Join-Path $RepoDir ".venv/bin/Activate.ps1")
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $activate) {
        throw "Virtual environment not found in $RepoDir\.venv. Create it first (see README)."
    }
    . $activate

    # 2) Credentials from the user's environment variables. A task started before you ran
    #    setx would not see them otherwise.
    foreach ($name in @("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "JOBHUNT_BOT_DAILY_AT")) {
        if (-not (Get-Item -Path "Env:$name" -ErrorAction SilentlyContinue)) {
            $value = [Environment]::GetEnvironmentVariable($name, "User")
            if ($value) { Set-Item -Path "Env:$name" -Value $value }
        }
    }
    if (-not $env:TELEGRAM_BOT_TOKEN -or -not $env:TELEGRAM_CHAT_ID) {
        throw "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set. See README: Telegram bot, step 3."
    }

    # 3) UTF-8 so accents print correctly in the log.
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

    # 4) Run the bot; restart it if it crashes, give up if it cannot stay up.
    $botArgs = @("bot")
    if ($NoDaily) { $botArgs += "--no-daily" }
    elseif ($DailyAt) { $botArgs += @("--daily-at", $DailyAt) }
    $quickFailures = 0
    while ($true) {
        $started = Get-Date
        Write-Output ("[{0}] jobhunt {1}" -f (Get-Date -Format s), ($botArgs -join " "))
        & jobhunt @botArgs
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) { break }  # stopped on purpose (Ctrl+C)
        if (((Get-Date) - $started).TotalSeconds -lt 30) { $quickFailures++ } else { $quickFailures = 0 }
        if ($quickFailures -ge 5) {
            Write-Output "The bot keeps failing right after starting (exit code $exitCode); giving up."
            break
        }
        Write-Output ("[{0}] bot exited with code {1}; restarting in {2} s" -f (Get-Date -Format s), $exitCode, $RestartDelaySeconds)
        Start-Sleep -Seconds $RestartDelaySeconds
    }
}
catch {
    Write-Output "ERROR: $_"
    $exitCode = 1
}
finally {
    Stop-Transcript | Out-Null
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
exit $exitCode
