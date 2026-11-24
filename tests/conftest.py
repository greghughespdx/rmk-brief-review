"""Hermetic test environment.

Every test runs against the defaults plus these overrides, never against a
config file on the machine running the tests:

- RMW_CONFIG=none ignores any remarkable-workflow.toml or ~/.config file;
- the content root is the repository, so fixture "Read:" paths such as
  tests/fixtures/remarkable/reading-sample.md resolve the way they do in a
  real queue;
- all state (archive, registry, logs) lands in a throwaway directory;
- every hook is empty, so nothing is ever sent anywhere.

Shell suites started from tests/test_shell_suites.py inherit the same
environment.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_STATE = Path(tempfile.mkdtemp(prefix="rmw-tests-"))

os.environ["RMW_CONFIG"] = "none"
os.environ["RMW_PATHS_HOME"] = str(_STATE)
os.environ["RMW_PATHS_CONTENT_ROOT"] = str(ROOT)
os.environ["RMW_PATHS_QUEUE"] = str(ROOT / "tests" / "fixtures" / "queue-placeholder.md")
os.environ["RMW_PATHS_REGISTRY"] = str(_STATE / "registry.json")
os.environ["RMW_RENDER_PYTHON"] = sys.executable
for hook in ("ALERT", "NOTIFY", "PAGE", "ON_ANNOTATION", "TICKET_STATUS"):
    os.environ[f"RMW_HOOKS_{hook}"] = ""
# The ticket check is off by default; the suite exercises it with the beads
# id pattern (prefix-xxxx plus dotted child suffixes).
os.environ["RMW_TICKETS_PATTERN"] = r"\bmc-[a-z0-9]{2,6}(?:\.[0-9]+)*\b"
(_STATE / "archive" / "decisions").mkdir(parents=True, exist_ok=True)
os.environ.pop("PHONE_CARDS_URL", None)
os.environ.pop("REMARKABLE_TODAY_ARCHIVE", None)

# Tools import each other by bare module name, as they do when run in place.
sys.path.insert(0, str(ROOT / "remarkable"))
