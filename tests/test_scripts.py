from __future__ import annotations

import shutil
import subprocess

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
