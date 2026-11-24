#!/usr/bin/env python3
"""Write the launchd jobs that run the workflow on a schedule (macOS).

Jobs, with the config key that sets each time (see [schedule]):

  <prefix>.writer            schedule.writer     writer/author.py --apply
                             (only when writer.command is configured)
  <prefix>.review-preflight  schedule.preflight  scheduled-review-preflight.sh
  <prefix>.review            schedule.review     scheduled-review.sh
  <prefix>.morning-delivery  schedule.delivery   morning-dashboard.sh
  <prefix>.outcome-deadman   delivery + 60 min   morning-outcome-deadman.sh
  <prefix>.pen-chain-probe   delivery + 75 min   pen_chain_probe.py
  <prefix>.annotation-watch  every schedule.watch_interval_seconds
                             annotation-watch.sh

By default the plists are written to ./launchd-out for you to read. Pass
--install to copy them into ~/Library/LaunchAgents and load them with
launchctl bootstrap. The delivery and dead-man jobs set MB_ALLOW_OUTBOUND=1 and
MB_DEADMAN_ALLOW_OUTBOUND=1: without those flags they deliver nothing, by
design (see morning-dashboard.sh, "THE OUTBOUND GUARD").
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rmw_config  # noqa: E402


def _hm(text: str, plus_minutes: int = 0) -> dict[str, int]:
    hour, minute = (int(part) for part in text.split(":"))
    total = (hour * 60 + minute + plus_minutes) % (24 * 60)
    return {"Hour": total // 60, "Minute": total % 60}


def jobs(config: rmw_config.Config) -> dict[str, dict]:
    prefix = config.get("schedule.label_prefix")
    python = config.get("render.python")
    logs = Path(config.get("paths.log_dir"))
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + str(Path.home() / ".local/bin"),
        "HOME": str(Path.home()),
        "GIT_TERMINAL_PROMPT": "0",
    }
    config_file = rmw_config.config_path()
    if config_file is not None:
        env["RMW_CONFIG"] = str(config_file)
    delivery = config.get("schedule.delivery")

    def job(name, argv, when=None, interval=None, extra_env=None):
        plist = {
            "Label": f"{prefix}.{name}",
            "ProgramArguments": [str(item) for item in argv],
            "WorkingDirectory": str(HERE.parent),
            "EnvironmentVariables": {**env, **(extra_env or {})},
            "StandardOutPath": str(logs / f"{name}.log"),
            "StandardErrorPath": str(logs / f"{name}.log"),
        }
        if when is not None:
            plist["StartCalendarInterval"] = when
        if interval is not None:
            plist["StartInterval"] = int(interval)
        return plist

    out = {}
    if config.get("writer.command"):
        out["writer"] = job("writer", [python, HERE / "writer" / "author.py", "--apply"],
                            when=_hm(config.get("schedule.writer")))
    out["review-preflight"] = job("review-preflight", [HERE / "scheduled-review-preflight.sh"],
                                  when=_hm(config.get("schedule.preflight")))
    out["review"] = job("review", [HERE / "scheduled-review.sh"],
                        when=_hm(config.get("schedule.review")))
    out["morning-delivery"] = job("morning-delivery", [HERE / "morning-dashboard.sh"],
                                  when=_hm(delivery), extra_env={"MB_ALLOW_OUTBOUND": "1"})
    out["outcome-deadman"] = job("outcome-deadman", [HERE / "morning-outcome-deadman.sh"],
                                 when=_hm(delivery, 60),
                                 extra_env={"MB_DEADMAN_ALLOW_OUTBOUND": "1"})
    out["pen-chain-probe"] = job("pen-chain-probe", [python, HERE / "pen_chain_probe.py"],
                                 when=_hm(delivery, 75))
    out["annotation-watch"] = job("annotation-watch", [HERE / "annotation-watch.sh"],
                                  interval=config.get("schedule.watch_interval_seconds"))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("launchd-out"))
    parser.add_argument("--install", action="store_true",
                        help="copy into ~/Library/LaunchAgents and load with launchctl")
    args = parser.parse_args(argv)
    config = rmw_config.load()
    Path(config.get("paths.log_dir")).mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    written = []
    for plist in jobs(config).values():
        path = args.out / f"{plist['Label']}.plist"
        with path.open("wb") as handle:
            plistlib.dump(plist, handle)
        written.append(path)
        print(path)
    if args.install:
        agents = Path.home() / "Library" / "LaunchAgents"
        domain = f"gui/{os.getuid()}"
        for path in written:
            target = agents / path.name
            subprocess.run(["launchctl", "bootout", domain, str(target)], capture_output=True)
            shutil.copy2(path, target)
            subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
            print(f"loaded {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
