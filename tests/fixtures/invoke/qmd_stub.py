# Stub `qmd`: `collection list` names both session collections; every other call
# succeeds. Each call is appended to $QMD_LOG when set.
import os
import sys

if os.environ.get("QMD_LOG"):
    with open(os.environ["QMD_LOG"], "a") as handle:
        handle.write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1:3] == ["collection", "list"]:
    print("qmd://codex-sessions/\nqmd://claude-code-sessions/")
