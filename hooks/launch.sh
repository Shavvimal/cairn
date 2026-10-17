#!/bin/sh
# Cairn SessionEnd launcher. POSIX sh only: nothing here needs Python to bootstrap.
#
# Resolution order (first match wins):
#   1. The engine and QMD environment recorded by `cairn hook install`
#      (hook-launcher.env).
#   2. `cairn` on the inherited PATH. An engine too old for `cairn hook` (2.3.x)
#      is detected and the legacy `cairn sync --hook` runs detached instead.
#   3. A python3 that can import cairn. /usr/bin/python3 is only tried when the
#      Xcode Command Line Tools are installed: without them it is a stub that
#      pops an install dialog.
#   4. A one-line error. The host discards SessionEnd output, so this is for
#      `claude --debug` / `cairn doctor`, not the user.
#
# Supported upgrade order is engine first (`uv tool upgrade cairn`), then plugin.
# Plugins auto-update, so step 2 keeps a violated order working, degraded.

launcher="${XDG_CONFIG_HOME:-$HOME/.config}/cairn/hook-launcher.env"
# A GUI host may omit or replace the XDG home used at installation. The installer
# keeps a discovery copy at this stable location; an existing host-specific file wins.
if [ ! -f "$launcher" ]; then
    launcher="$HOME/.config/cairn/hook-launcher.env"
fi
hint="run: uv tool upgrade cairn && cairn hook install"

if [ -f "$launcher" ]; then
    # shellcheck disable=SC1090
    . "$launcher"
    if [ -n "${CAIRN_PYTHON:-}" ] && [ -x "$CAIRN_PYTHON" ]; then
        export CAIRN_CONFIG
        PATH="${CAIRN_PATH:-$PATH}"
        export PATH
        exec "$CAIRN_PYTHON" -m cairn hook handle "$@"
    fi
    echo "cairn hook: recorded interpreter is missing; $hint" >&2
fi

if command -v cairn >/dev/null 2>&1; then
    err=$(cairn hook handle "$@" 2>&1 >/dev/null)
    code=$?
    case "$code:$err" in
        2:*"unknown command 'hook'"*)
            # 2.3.x engine behind a 2.4 plugin: keep syncing the old way, detached.
            nohup cairn sync --hook >/dev/null 2>&1 &
            echo "cairn hook: engine is older than the plugin, used legacy sync; $hint" >&2
            exit 0
            ;;
    esac
    [ -n "$err" ] && printf '%s\n' "$err" >&2
    exit "$code"
fi

for python in "$(command -v python3 2>/dev/null)" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    [ -n "$python" ] && [ -x "$python" ] || continue
    if [ "$python" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then
        continue
    fi
    if "$python" -c 'import cairn' >/dev/null 2>&1; then
        exec "$python" -m cairn hook handle "$@"
    fi
done

echo "cairn hook: no cairn engine found; $hint" >&2
exit 1
