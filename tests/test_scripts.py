from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest


def test_run_daily_script_contents(root):
    script = (root / "scripts" / "run_daily.ps1").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\Activate.ps1" in script
    assert '@("run", "--notify", "telegram"' in script and '"--prepare"' in script
    assert "TELEGRAM_BOT_TOKEN" in script and "TELEGRAM_CHAT_ID" in script
    assert "Start-Transcript" in script and "exit $exitCode" in script


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell not installed")
def test_run_daily_script_parses(root):
    command = (
        "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{root / 'scripts' / 'run_daily.ps1'}', [ref]$null, [ref]$e); exit $e.Count"
    )
    result = subprocess.run(["pwsh", "-NoProfile", "-Command", command], capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_start_bot_script_contents(root):
    script = (root / "scripts" / "start_bot.ps1").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\Activate.ps1" in script
    assert '$botArgs = @("bot")' in script and "& jobhunt @botArgs" in script
    assert "TELEGRAM_BOT_TOKEN" in script and "TELEGRAM_CHAT_ID" in script
    assert "Local\\job-hunt-copilot-bot" in script  # one bot at a time
    assert "Start-Transcript" in script and "exit $exitCode" in script


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell not installed")
def test_start_bot_script_parses(root):
    command = (
        "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{root / 'scripts' / 'start_bot.ps1'}', [ref]$null, [ref]$e); exit $e.Count"
    )
    result = subprocess.run(["pwsh", "-NoProfile", "-Command", command], capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def fake_repo(tmp_path, root):
    """A throw-away repository whose `jobhunt` is a shell stub that records its arguments."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(root / "scripts" / "start_bot.ps1", tmp_path / "scripts" / "start_bot.ps1")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "Activate.ps1").write_text(
        f'$env:PATH = "{venv_bin}" + [IO.Path]::PathSeparator + $env:PATH\n', encoding="utf-8"
    )
    stub = venv_bin / "jobhunt"
    stub.write_text(
        '#!/bin/sh\necho "$@" >> "$STUB_LOG"\nexit "${STUB_EXIT:-0}"\n', encoding="utf-8"
    )
    stub.chmod(0o755)
    return tmp_path


def run_start_bot(repo, *args, **env):
    full_env = {
        **os.environ,
        "STUB_LOG": str(repo / "calls.log"),
        "TELEGRAM_BOT_TOKEN": "123:fake",
        "TELEGRAM_CHAT_ID": "42",
        **env,
    }
    return subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(repo / "scripts" / "start_bot.ps1"), *args],
        capture_output=True, text=True, env=full_env, cwd=repo, timeout=120,
    )  # fmt: skip


pwsh_only = pytest.mark.skipif(
    shutil.which("pwsh") is None or os.name == "nt", reason="needs PowerShell on Linux/macOS"
)


@pwsh_only
def test_start_bot_runs_jobhunt_bot_and_logs(fake_repo):
    result = run_start_bot(fake_repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (fake_repo / "calls.log").read_text().split() == ["bot"]
    assert list((fake_repo / "logs").glob("bot_*.log"))


@pwsh_only
def test_start_bot_passes_the_schedule_options(fake_repo):
    run_start_bot(fake_repo, "-DailyAt", "08:30")
    run_start_bot(fake_repo, "-NoDaily")
    assert (fake_repo / "calls.log").read_text().splitlines() == [
        "bot --daily-at 08:30",
        "bot --no-daily",
    ]


@pwsh_only
def test_start_bot_refuses_without_credentials(fake_repo):
    result = run_start_bot(fake_repo, TELEGRAM_BOT_TOKEN="")
    assert result.returncode == 1
    assert "TELEGRAM_BOT_TOKEN" in result.stdout and not (fake_repo / "calls.log").exists()


@pwsh_only
def test_start_bot_gives_up_when_the_bot_keeps_crashing(fake_repo):
    result = run_start_bot(fake_repo, "-RestartDelaySeconds", "0", STUB_EXIT="1")
    assert result.returncode == 1
    assert len((fake_repo / "calls.log").read_text().splitlines()) == 5
    assert "giving up" in result.stdout


@pwsh_only
def test_a_second_copy_exits_quietly(fake_repo):
    # the first "bot" runs for 8 s; the second start happens meanwhile
    (fake_repo / ".venv" / "bin" / "jobhunt").write_text(
        '#!/bin/sh\necho "$@" >> "$STUB_LOG"\nsleep 8\n', encoding="utf-8"
    )
    first = subprocess.Popen(
        ["pwsh", "-NoProfile", "-File", str(fake_repo / "scripts" / "start_bot.ps1")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=fake_repo,
        env={**os.environ, "STUB_LOG": str(fake_repo / "calls.log"),
             "TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_CHAT_ID": "42"},
    )  # fmt: skip
    deadline = time.time() + 30  # wait until the first copy is really running the bot
    while not (fake_repo / "calls.log").exists() and time.time() < deadline:
        time.sleep(0.2)
    second = run_start_bot(fake_repo)
    first.wait(timeout=60)
    assert second.returncode == 0 and "already running" in second.stdout
    assert (fake_repo / "calls.log").read_text().split() == ["bot"]  # only one bot ever ran
