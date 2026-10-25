"""
Configuration loading.

Regression tests for a failure that only appeared in production: the scheduled
runs worked in every manual test and failed the first time launchd ran them.
"""

import os
import subprocess
import sys
from pathlib import Path


def test_api_key_resolves_without_a_shell_environment():
    """
    launchd starts processes with a bare environment -- no shell, nothing
    sources .env. The Anthropic SDK reads ANTHROPIC_API_KEY from os.environ,
    so a key that only exists in the shell profile vanishes exactly when the
    scheduler needs it. Settings must read it from .env itself.
    """
    project = Path(__file__).resolve().parent.parent
    if not (project / ".env").exists():
        import pytest

        pytest.skip("no .env in this checkout")

    result = subprocess.run(
        [sys.executable, "-c",
         "from config import get_settings; "
         "print(bool(get_settings().resolved_anthropic_key))"],
        cwd=project,
        env={"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.stdout.strip() == "True", (
        f"key did not resolve in a bare environment: {result.stdout} {result.stderr}"
    )
