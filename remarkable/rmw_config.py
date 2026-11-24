#!/usr/bin/env python3
"""Configuration loader for remarkable-workflow.

Every path, command, folder name and integration hook the workflow uses comes
from one TOML file. Nothing else in the repository hard-codes a machine, a
person or a service.

Where the file is looked for, first match wins:

  1. the path in the RMW_CONFIG environment variable;
  2. ./remarkable-workflow.toml in the current directory;
  3. ~/.config/remarkable-workflow/config.toml.

No file at all is fine: every key has a default (see DEFAULTS below and
config.example.toml). An environment variable always beats the file, so tests
and one-off runs can override a single value without editing anything.

Shell scripts read the same values through `rmw-env.sh`, which runs
`python3 rmw_config.py shell` and exports each value as an RMW_* variable
(only when that variable is not already set).

Usage:
  rmw_config.py shell        print `: "${RMW_X:=...}"; export RMW_X` lines
  rmw_config.py show         print the resolved configuration as JSON
  rmw_config.py get KEY      print one resolved value (for example tablet.rmapi)
  rmw_config.py init         create the state folders and a starter queue
  rmw_config.py hook NAME .. run one configured hook (alert, notify, page, ...)
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - documented requirement
    tomllib = None  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

# Every key the workflow reads, with its default. Keep this in step with
# config.example.toml; the README documents each one.
DEFAULTS: dict[str, dict[str, Any]] = {
    "paths": {
        # Root for all local state the workflow writes.
        "home": "~/.local/share/remarkable-workflow",
        # The queue file: a Markdown table of decision cards (see README).
        "queue": "{home}/decisions-queue.md",
        # Read:, Facts: and Specimen: paths on a card resolve under this root.
        # Empty means "the directory that holds the queue file".
        "content_root": "",
        # Archive of every rendered document, manifest, receipt and pen read.
        "archive": "{home}/archive",
        # Registry of documents pushed to the tablet that the watcher resolves.
        "registry": "~/.remarkable-today-pages.json",
        # Where logs go.
        "log_dir": "{home}/logs",
    },
    "tablet": {
        # The rmapi binary (https://github.com/ddvk/rmapi or a fork).
        "rmapi": "rmapi",
        # Folder that holds the current briefs and the one Decisions document.
        "briefs_folder": "/Briefs",
        # Where read or answered documents are filed.
        "previous_folder": "/Briefs/Previous Briefs",
        # Days a daily brief stays in the previous folder before it is pruned.
        "keep_days": 7,
    },
    "render": {
        # Python with reportlab, rmscene and Pillow installed. Empty = this one.
        "python": "",
        # Brief engine: "chrome" (pandoc + headless Chrome) or "reportlab".
        "brief_engine": "chrome",
        "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "pandoc": "pandoc",
        "pdftoppm": "pdftoppm",
    },
    "writer": {
        # argv for the LLM that rewrites cards. With protocol "plain" the
        # prompt arrives on stdin and a JSON array must come back on stdout.
        # With "agy-stream-json" command[0] is the Gemini agy CLI.
        # Empty = the writer step is off (cards are used as written).
        "command": [],
        "protocol": "plain",
        # Model name for the agy-stream-json protocol. Empty = its default.
        "model": "",
        # Name recorded as the document's producer. The reviewer must differ.
        "identity": "writer",
        "timeout_seconds": 280,
        # Commit the rewritten queue with git when the queue lives in a repo.
        "git_commit": False,
    },
    "reviewer": {
        # argv for the independent reviewer. Placeholders: {stage_dir},
        # {prompt}, {prompt_file}, {images} (expands to every page image, each
        # preceded by image_flag when image_flag is set).
        "command": [],
        "identity": "independent-reviewer",
        "image_flag": "",
        "timeout_seconds": 2400,
        # Last wall-clock time (HH:MM) a scheduled review may start, so it can
        # never run into the delivery time (schedule.delivery).
        "cutoff": "05:40",
    },
    "hooks": {
        # Each hook is an argv list. Empty = no-op. Arguments are appended.
        # alert: subject, body, slug. For operators: something failed.
        "alert": [],
        # notify: one line for the reader (the morning outcome, a question
        # about an unclear pen mark).
        "notify": [],
        # page: an urgent push to a human operator (a phone push service).
        # Called as: --title T --body B --level L --source S
        "page": [],
        # on_annotation: the changed device paths, one per argument.
        "on_annotation": [],
        # ticket_status: prints a JSON object {ticket_id: status} on stdout.
        "ticket_status": [],
    },
    "tickets": {
        # Regex for ticket ids a card may cite. Empty = ticket check off.
        "pattern": "",
    },
    "briefing": {
        # The morning briefing is the "## Morning Briefing" section of a
        # Markdown file written overnight (by you, a script or an agent).
        # source_dir is searched for handoff_pattern ({date} = YYYY-MM-DD).
        # When source_dir is a git checkout, origin/main is preferred.
        "source_dir": "{home}/briefings",
        "handoff_pattern": "{date}-overnight.md",
        # argv printing Google Calendar style JSON ({"items": [...]}) for today.
        # Called with --time-min ISO --time-max ISO. Empty = try `gws`, then skip.
        "calendar_command": [],
        # Status lines on the briefing: "Name|URL|any" (any = any HTTP answer
        # counts as up). Empty = no service-health section.
        "health_probes": [],
        # Forecast location for the weather line (open-meteo.com). Empty = none.
        "weather_lat": "",
        "weather_lon": "",
    },
    "schedule": {
        "label_prefix": "com.example.remarkable-workflow",
        "writer": "03:15",
        "preflight": "03:30",
        "review": "05:00",
        "delivery": "06:00",
        "watch_interval_seconds": 120,
    },
}

# Environment variable for each key: RMW_<SECTION>_<KEY>, upper case. A few
# keys also have a short historical alias the scripts use.
ALIASES = {
    ("tablet", "rmapi"): "RMW_RMAPI_BIN",
    ("paths", "archive"): "RMW_ARCHIVE",
    ("paths", "queue"): "RMW_QUEUE",
}


def env_name(section: str, key: str) -> str:
    return f"RMW_{section}_{key}".upper()


class ConfigError(ValueError):
    pass


def config_path() -> Path | None:
    explicit = os.environ.get("RMW_CONFIG", "").strip()
    if explicit == "none":  # tests and hermetic runs: defaults and env only
        return None
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"RMW_CONFIG points at {path}, which does not exist")
        return path
    for candidate in (
        Path.cwd() / "remarkable-workflow.toml",
        Path.home() / ".config" / "remarkable-workflow" / "config.toml",
    ):
        if candidate.is_file():
            return candidate
    return None


def _coerce(value: str, default: Any) -> Any:
    if isinstance(default, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(value)
    if isinstance(default, list):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                raise ConfigError(f"expected a JSON list, got {text!r}")
            return [str(item) for item in parsed]
        return shlex.split(text)
    return value


def load_raw() -> dict[str, dict[str, Any]]:
    data = copy.deepcopy(DEFAULTS)
    path = config_path()
    if path is not None:
        if tomllib is None:
            raise ConfigError("Python 3.11 or newer is needed to read the TOML config")
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
        for section, values in loaded.items():
            if section not in data or not isinstance(values, dict):
                raise ConfigError(f"unknown config section [{section}] in {path}")
            for key, value in values.items():
                if key not in data[section]:
                    raise ConfigError(f"unknown config key {section}.{key} in {path}")
                data[section][key] = value
    for section, values in data.items():
        for key, default in list(values.items()):
            for name in (env_name(section, key), ALIASES.get((section, key))):
                if name and name in os.environ:
                    values[key] = _coerce(os.environ[name], DEFAULTS[section][key])
                    break
    return data


def _expand(text: str, scope: dict[str, str]) -> str:
    for _ in range(5):
        new = text.format(**scope) if "{" in text else text
        if new == text:
            break
        text = new
    return str(Path(text).expanduser()) if text.startswith("~") else text


def resolve(data: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = copy.deepcopy(data)
    paths = out["paths"]
    scope: dict[str, str] = {}
    for key in ("home", "archive", "queue", "registry", "log_dir", "content_root"):
        value = str(paths[key])
        if value:
            value = _expand(value, scope)
        scope[key] = value
        paths[key] = value
    if not paths["content_root"]:
        paths["content_root"] = str(Path(paths["queue"]).expanduser().parent)
    out["briefing"]["source_dir"] = _expand(str(out["briefing"]["source_dir"]), scope)
    if not out["render"]["python"]:
        venv = PROJECT_ROOT / ".venv" / "bin" / "python"
        out["render"]["python"] = str(venv) if venv.exists() else sys.executable
    return out


@dataclass(frozen=True)
class Config:
    data: dict[str, dict[str, Any]]

    def get(self, dotted: str) -> Any:
        section, key = dotted.split(".", 1)
        return self.data[section][key]

    @property
    def home(self) -> Path:
        return Path(self.data["paths"]["home"])

    @property
    def queue(self) -> Path:
        return Path(self.data["paths"]["queue"])

    @property
    def content_root(self) -> Path:
        return Path(self.data["paths"]["content_root"])

    @property
    def archive(self) -> Path:
        return Path(self.data["paths"]["archive"])

    @property
    def decisions_archive(self) -> Path:
        """Per-day folders of rendered Decisions documents."""
        return self.archive / "decisions"

    @property
    def question_state(self) -> Path:
        """Durable per-question answer state. It always sits at the archive
        root, beside the decisions/ folder, because the preflight review
        relocates both together into a scratch copy."""
        return self.archive / "question-state.json"

    @property
    def registry(self) -> Path:
        return Path(self.data["paths"]["registry"])

    def hook(self, name: str) -> list[str]:
        value = self.data["hooks"].get(name) or []
        return [str(item) for item in value]


def load() -> Config:
    return Config(resolve(load_raw()))


def run_hook(name: str, *args: str, timeout: int = 60, dry_run: bool = False) -> dict[str, Any]:
    """Run a configured hook. A hook that is not configured is a no-op success."""
    import subprocess

    argv = load().hook(name)
    if not argv:
        return {"sent": False, "configured": False}
    if dry_run:
        return {"sent": False, "configured": True, "dry_run": True, "argv": argv + list(args)}
    try:
        completed = subprocess.run(
            argv + list(args), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"sent": False, "configured": True, "error": f"{type(exc).__name__}: {exc}"}
    result: dict[str, Any] = {"sent": completed.returncode == 0, "configured": True,
                              "returncode": completed.returncode}
    if completed.returncode:
        result["error"] = (completed.stderr or completed.stdout).strip()[:500]
    return result


def _shell_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, list):
        return json.dumps([str(item) for item in value])
    return str(value)


def shell_lines(config: Config) -> list[str]:
    def line(name: str, value: str) -> str:
        # Set only when the variable is not already in the environment, so an
        # explicit override (even an empty one) always wins.
        return f'[ -n "${{{name}+x}}" ] || {name}={shlex.quote(value)}; export {name}'

    lines = [line("RMW_PROJECT_ROOT", str(PROJECT_ROOT))]
    for section, values in config.data.items():
        for key, value in values.items():
            lines.append(line(env_name(section, key), _shell_value(value)))
    derived = {
        "RMW_DECISIONS_ARCHIVE": str(config.decisions_archive),
        "RMW_ARCHIVE": str(config.archive),
        "RMW_QUESTION_STATE": str(config.question_state),
    }
    for name, value in derived.items():
        lines.append(line(name, value))
    return lines


STARTER_QUEUE = """# Decisions queue

One row per decision card, under a heading that starts with "## Active".
See CARD-WRITING.md for the format and examples/decisions-queue.md for a
worked example. Rows under any other heading are kept but never printed.

## Active - Decisions

| Item | Project | Age | Ask |
| --- | --- | --- | --- |

## Closed

| Item | Closed | Disposition |
| --- | --- | --- |
"""


def init(config: Config) -> int:
    """Create the local folders and a starter queue. Never overwrites."""
    made = []
    for folder in (config.home, config.decisions_archive, Path(config.get("paths.log_dir")),
                   Path(config.get("briefing.source_dir"))):
        if not folder.exists():
            folder.mkdir(parents=True)
            made.append(str(folder))
    if not config.queue.exists():
        config.queue.parent.mkdir(parents=True, exist_ok=True)
        config.queue.write_text(STARTER_QUEUE, encoding="utf-8")
        made.append(str(config.queue))
    for item in made:
        print(f"created {item}")
    if not made:
        print("everything already exists; nothing changed")
    return 0


def main(argv: list[str]) -> int:
    try:
        config = load()
    except (ConfigError, OSError, ValueError) as exc:
        print(f"rmw_config: {exc}", file=sys.stderr)
        return 2
    if len(argv) >= 2 and argv[1] == "shell":
        print("\n".join(shell_lines(config)))
        return 0
    if len(argv) >= 2 and argv[1] == "show":
        print(json.dumps(config.data, indent=2))
        return 0
    if len(argv) == 3 and argv[1] == "get":
        value = config.get(argv[2])
        print(_shell_value(value))
        return 0
    if len(argv) == 2 and argv[1] == "init":
        return init(config)
    if len(argv) >= 3 and argv[1] == "hook":
        name = argv[2]
        if name not in DEFAULTS["hooks"]:
            print(f"rmw_config: unknown hook {name!r}", file=sys.stderr)
            return 2
        outcome = run_hook(name, *argv[3:])
        if not outcome["configured"]:
            print(f"rmw_config: hook '{name}' is not configured; nothing sent", file=sys.stderr)
            # An operator alert with no listener is not a failure of the run.
            # A message meant for the reader that reached nobody is reported
            # as not delivered (exit 3), so callers never record it as sent.
            return 0 if name in ("alert", "on_annotation", "ticket_status") else 3
        if not outcome["sent"]:
            print(f"rmw_config: hook '{name}' failed: {outcome.get('error', '')}", file=sys.stderr)
            return 1
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
