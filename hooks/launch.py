"""Minimal bootstrap: read setup's absolute interpreter/config, then exec Cairn.

This file intentionally uses only syntax available in the system Python 3.
The installed engine's interpreter supplies Python >=3.11. No network/bootstrap
installation is attempted inside a lifecycle callback.
"""

import json
import os
import pathlib
import sys


def main():
    directory = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config")
    path = directory / "cairn" / "hook-launcher.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        python = settings["python"]
        if not pathlib.Path(python).is_absolute() or not pathlib.Path(python).is_file():
            raise ValueError("installed interpreter is missing; rerun cairn hook install")
        env = dict(os.environ)
        env["CAIRN_CONFIG"] = settings["config"]
        env["PATH"] = settings["path"]
        os.execve(python, [python, "-m", "cairn", "hook", "handle", *sys.argv[1:]], env)
    except (OSError, ValueError, KeyError) as exc:
        print("Cairn hook unavailable: {}; run cairn hook install".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
