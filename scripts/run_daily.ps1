<#
.SYNOPSIS
    Daily job-hunt-copilot run for Windows Task Scheduler.

.DESCRIPTION
    Activates the project's virtual environment and runs
        jobhunt run --notify telegram --prepare
    which discovers new postings, scores them, prepares CV/letter/LinkedIn note
    for the new good matches and sends them to YOUR Telegram chat.
    Nothing is ever sent to employers or LinkedIn.

    Telegram credentials are read from the environment variables
    TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (user scope). They are never stored in
    the repository. Without them the run still works and simply sends nothing.

    Output of every run goes to logs\run_<date>.log (kept for 30 days); the shortlist
    is also saved as output\shortlist_<date>.md.

.PARAMETER RepoDir
    Repository root. Defaults to the folder that contains this scripts\ directory.

.PARAMETER NoPrepare
    Only notify; do not generate CV/letter/LinkedIn note.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\path\to\job-hunt-copilot\scripts\run_daily.ps1
#>
[CmdletBinding()]
param(
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot),
    [switch]$NoPrepare
)

$ErrorActionPreference = "Stop"
$exitCode = 1
Set-Location -Path $RepoDir

# Logs: one file per run, older than 30 days removed.
$logDir = Join-Path $RepoDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Get-ChildItem -Path $logDir -Filter "run_*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue
$stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
Start-Transcript -Path (Join-Path $logDir "run_$stamp.log") -Append | Out-Null

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

    # 2) Telegram credentials from the user's environment variables. A scheduled task
    #    started before you ran setx would not see them otherwise.
    foreach ($name in @("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")) {
        if (-not (Get-Item -Path "Env:$name" -ErrorAction SilentlyContinue)) {
            $value = [Environment]::GetEnvironmentVariable($name, "User")
            if ($value) { Set-Item -Path "Env:$name" -Value $value }
        }
    }
    if (-not $env:TELEGRAM_BOT_TOKEN -or -not $env:TELEGRAM_CHAT_ID) {
        Write-Warning "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: running without notifications."
    }

    # 3) UTF-8 so accents and symbols print correctly in the log.
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

    # 4) The run itself.
    $shortlist = Join-Path (Join-Path $RepoDir "output") ("shortlist_{0}.md" -f (Get-Date -Format "yyyy-MM-dd"))
    $jobArgs = @("run", "--notify", "telegram", "--export", $shortlist)
    if (-not $NoPrepare) { $jobArgs += "--prepare" }
    Write-Output ("[{0}] jobhunt {1}" -f (Get-Date -Format s), ($jobArgs -join " "))
    & jobhunt @jobArgs
    $exitCode = $LASTEXITCODE
}
catch {
    Write-Output "ERROR: $_"
    $exitCode = 1
}
finally {
    Stop-Transcript | Out-Null
}
exit $exitCode
