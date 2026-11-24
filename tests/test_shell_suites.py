"""Run the shell test suites that sit next to the scripts they test.

Each suite drives the real script against fakes at the network boundary (a
fake rmapi over a fixture device tree, a fake reviewer, a fake alert hook) and
prints PASS/FAIL lines. A suite passes only when it exits 0.
"""
import os
import subprocess
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "remarkable"
SUITES = sorted(TOOLS.glob("test-*.sh"))


@pytest.mark.parametrize("suite", SUITES, ids=[suite.name for suite in SUITES])
def test_shell_suite(suite):
    result = subprocess.run(["bash", str(suite)], capture_output=True, text=True,
                            timeout=900, env=dict(os.environ), cwd=str(TOOLS.parent))
    tail = "\n".join((result.stdout + result.stderr).splitlines()[-40:])
    assert result.returncode == 0, f"{suite.name} exited {result.returncode}\n{tail}"
